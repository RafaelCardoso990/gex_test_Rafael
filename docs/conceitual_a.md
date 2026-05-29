# Conceitual — Parte A: Resolução de incidente

**Cenário:** sexta o gateway reporta US$ 1.3M / 1.587 transações aprovadas; em `lead_events` temos só
421 `order.approved` no mesmo período. Call center sem leads há 4h.

## 1. Primeira ação (antes de tocar em produção)
Não agir no impulso. Em ordem: (a) **acknowledge ao PO** com ETA de diagnóstico e abrir incidente;
(b) **confirmar o escopo** — janela exata, fuso (UTC vs local), e a definição de "aprovada" do
dashboard deles; (c) **checar a saúde AGORA** — "sem leads há 4h" sugere parada *ativa*, não só perda
de sexta; (d) **preservar evidências** (não limpar filas/logs) — nosso `raw_payloads` guarda todo
payload recebido, então nada foi perdido de fato e dá pra reprocessar. Só depois, diagnosticar.

## 2. Cinco hipóteses ranqueadas (probabilidade ↓)
1. **Consumer parado/morto** — "4h sem leads" é sintoma de parada contínua; mensagens empilhando em
   `lead.received` sem ninguém consumir. Mais provável pelo sintoma.
2. **Discrepância de medição** — fuso da "sexta", ou o dashboard conta status/duplicatas que nós
   filtramos (refunded/declined, retries). Barato de descartar e muito comum; sempre verificar antes
   de assumir perda real.
3. **Decrypt em massa falhando** — gateway rotacionou a chave grummer ou mudou o formato → tudo em
   `lead.dead.decrypt_failed`. Provável se a maioria do volume é grummer.
4. **Lentidão (backlog)** — consumer vivo mas com lag enorme; fila crescendo mais rápido que drena.
5. **Schema mudou** — novo campo/obrigatório → `lead.dead.schema_failed` em massa.

## 3. Dados/queries/comandos que eu olharia primeiro
**Saúde da fila (isola hipótese 1 e 4):**
```bash
rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
# lead.received com consumers=0  -> consumer morto (cenário c)
# messages_ready alto e subindo  -> backlog/parada
```
**Reconciliação por camada — `raw_payloads` é a fonte da verdade** (já diferencia "problema nosso" de
"problema do gateway"):
```sql
SELECT processing_status, COUNT(*) FROM raw_payloads
WHERE received_at >= '2026-05-23' AND received_at < '2026-05-24'
GROUP BY processing_status;        -- ~1587 recebidos? quantos 'routed' vs decrypt/schema/duplicate?
SELECT source, COUNT(*) FROM lead_dead_letter
WHERE created_at >= '2026-05-23' AND created_at < '2026-05-24' GROUP BY source;
```
**Rastro por `correlation_id`:** pego um `transaction_id` que o gateway diz ter enviado e que não está
em `lead_events`, acho em `raw_payloads`, extraio o `correlation_id` e sigo o rastro nos logs
estruturados (receiver → consumer → distribuidor) pra ver em que camada parou.

## 4. Diferenciando os cenários (a/b/c/d)
A combinação `raw_payloads` × DLQ × `idempotency_keys` × `lead_events` × fila localiza a camada exata:

| Cenário | raw_payloads | DLQ | idempotency_keys | lead_events | fila |
|---|---|---|---|---|---|
| (a) gateway nunca enviou | **ausente** | — | — | ausente | — |
| (b) decrypt falhou | presente (`decrypt_failed`) | `...decrypt_failed` | — | ausente | — |
| (c) publicado, consumer travou | presente (`routed`) | — | **presente** | **ausente** | `lead.received` ready>0, consumers=0 |
| (d) consumer ok, distribuidor não | presente (`routed`) | — | presente | **presente** | `dist.sms` ready>0, consumers=0; `distribution_status`=pending |

Ou seja: se não está em `raw_payloads` → problema do gateway/endpoint (a). Se está mas não virou
`lead_events` → nosso, e a fila/idempotência dizem se foi consumer (c) ou distribuição (d).

## 5. Reprocessar os 1.166 sem duplicar os 421
**A idempotência torna o reprocessamento seguro por design:** a chave natural `(transaction_id, event)`
em `idempotency_keys` e `lead_events` barra qualquer reentrada dos 421 já processados — re-injetar tudo
é seguro, os existentes viram `duplicate` na `sp_insert_lead`. Não preciso de `DELETE` nem de lógica
especial de dedup.

- **Se for cenário (c)** (mensagens presas em `lead.received`): subir/consertar o consumer → ele drena
  a fila sozinho; reentregas duplicadas são absorvidas pela UNIQUE de `lead_events`.
- **Se for (b)/(d)** (na DLQ): corrigir a causa-raiz primeiro (ex.: nova chave grummer no receiver),
  depois **republicar da DLQ** de forma controlada (script com rate limit, não shovel cego).
- **Identificar os faltantes** comparando a lista do gateway com a nossa:
```sql
-- gateway_friday(transaction_id) = os 1587 IDs exportados do dashboard deles
SELECT g.transaction_id FROM gateway_friday g
LEFT JOIN orders o  ON o.transaction_id = g.transaction_id
LEFT JOIN lead_events le ON le.order_id = o.id AND le.event = 'order.approved'
WHERE le.id IS NULL;             -- candidatos a reprocessar (idempotência cobre o resto)
```
- Os que nem estão em `raw_payloads` (cenário a): pedir replay ao gateway; idempotência cobre.
- **Validar:** rodar a query de reconciliação até o gap `1587 - count(lead_events approved)` ir a ~0.

## 6. Três medidas preventivas
1. **Alerta de fila:** `lead.received` com `consumers == 0` ou `messages_ready` acima de um limiar por
   > N min → dispara. Pega o cenário (c) em minutos, não em 4h.
2. **Reconciliação automática contínua:** job periódico comparando `raw_payloads` recebidos × `lead_events`
   × total do gateway por janela; alerta se o gap passar de um threshold (detecta perda proativamente).
3. **Alerta de taxa de DLQ (Prometheus):** spike em `decrypt_failed`/`schema_failed` sinaliza chave
   rotacionada ou mudança de schema **antes** de virar incidente; + liveness dos workers com restart
   automático.
