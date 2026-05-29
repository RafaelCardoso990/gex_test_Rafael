# Documento explicativo — Esteira de Integração GEX

## Visão geral do fluxo
Um webhook chega em `POST /webhooks/{gateway}`. O receiver: (1) gera um `correlation_id` e grava o
payload bruto em `raw_payloads` **antes de qualquer processamento** (auditoria-primeiro); (2) decripta
(grummer, AES-256-CBC/PKCS7) ou lê direto (lous); (3) valida o schema; (4) normaliza e-mail/telefone/nome;
(5) garante idempotência pela chave natural `(transaction_id, event)`; (6) roteia: aprovado → fila
`lead.received`; decrypt/schema inválido → DLQ correspondente; status ≠ approved → descarta (só auditoria).
O **consumer** consome `lead.received`, persiste lead+order+evento de forma atômica via `sp_insert_lead`
(calculando o lag gateway→DB), cria os 4 canais em `distribution_status` e publica `dist.*`. O
**distribuidor SMS** consome `dist.sms`, faz POST ao canal (10% de falha simulada), com retry/backoff e
DLQ; no sucesso marca `delivered` e calcula o lag DB→canal. Resultado no dataset: **125 leads** únicos,
35 em DLQ (15 decrypt + 20 schema), 20 descartados, 20 duplicados barrados — igual ao gabarito.

## Linguagem e bibliotecas
**Python 3.12 + FastAPI.** O gargalo da esteira é I/O (banco, fila, cripto), não CPU — async em Python
atende bem, com entrega mais rápida e legível no prazo. Bibliotecas: **cryptography** (AES sem
gambiarra), **pydantic v2** (validação declarativa com erro estruturado → vai pronto pra DLQ),
**aio-pika** (RabbitMQ async; topic/direct + DLQ), **aiomysql** (SQL puro, controle fino de índices/
EXPLAIN/stored proc — exatamente o que o teste cobra), **structlog** (logs JSON), **phonenumbers**
(E.164 confiável). Testes: **pytest + testcontainers** (MySQL/RabbitMQ efêmeros). Empacotamento com
**uv**. SQL puro em vez de ORM: a avaliação pede EXPLAIN e stored procedure, e ORM esconderia o SQL.

## Decisões importantes
- **Arquitetura hexagonal:** domínio puro (decrypt, validação, normalização, roteamento) isolado dos
  adapters de I/O. Resultado: 51 dos 74 testes rodam sem infra, em ~0,15s.
- **Idempotência em 2 camadas:** `idempotency_keys` no receiver (INSERT atômico + captura de violação
  da UNIQUE — **sem** SELECT-antes, então é race-safe; testado com 20 requisições concorrentes → 1
  vencedor) e `lead_events` UNIQUE no consumer (blinda contra reentrega, pois RabbitMQ é *at-least-once*).
- **`raw_payloads` é sagrado:** gravado antes de processar e sem FK/UNIQUE — guarda até o que falha no
  decrypt (que não tem `transaction_id`), viabilizando reprocessamento.
- **DLQ por motivo** (`decrypt_failed`/`schema_failed`/`consumer_failed`/`dist.dead.sms`): reprocessar
  é diferente por causa-raiz.
- **`sp_insert_lead` transacional:** lead + order + evento + 4 canais numa transação só (sem estado
  parcial); o que sai pra fila acontece só após o commit.
- **PII nunca em log:** e-mail/telefone só como `cust_<hash sha256>`.
- **Retry no consumer/distribuidor:** backoff 1s/4s/16s in-process por simplicidade — *trade-off:*
  bloqueia o worker; em produção usaria delayed-exchange (TTL+DLX). Registrado.

## Justificativa dos índices (`002_indexes.sql`)
Cada índice serve a uma query de auditoria (`audit_queries.sql`); o EXPLAIN no dataset usa `ref`/`eq_ref`,
sem full scan custoso. Em volume baixo o otimizador pode varrer (tabelas minúsculas, < 1s de qualquer
forma); os índices são dimensionados para o volume de produção (milhares/dia).

| Índice | Tabela | Query que serve |
|---|---|---|
| `(channel, status, delivered_at)` | distribution_status | Q1 lag médio SMS por gateway (24h) |
| `(status, created_at)` | distribution_status | Q2 pendentes > 5 min |
| `(event, persisted_at)` | lead_events | Q5 reconciliação aprovados/dia |
| `(source, created_at)` | lead_dead_letter | Q4 DLQ por motivo (24h) |
| `(gateway, received_at)` | raw_payloads | auditoria/incidente por gateway |

As UNIQUE (`idempotency_keys`, email, `(gateway, transaction_id)`, `(order_id, event)`,
`(order_id, channel)`) garantem integridade **e** servem de índice para lookups por chave natural.

## Premissas registradas
- **E-mail inválido → quarentena** (`lead.dead.schema_failed`): não entra em `leads` (que tem UNIQUE em
  e-mail). Telefone inválido apenas marca `phone_valid=0` e o lead segue.
- **`first_name` vazio → `"Customer"`** (default na normalização, para não quebrar o downstream).
- **`idempotency_keys` é uma 7ª tabela** além das 6 mínimas, necessária para responder `duplicate` de
  forma síncrona e race-safe no receiver.
- **Os 4 canais são criados na própria `sp_insert_lead`** (atomicidade total); só o SMS é distribuído.
- **Tudo em UTC** no banco; lag em segundos.
- **Canal SMS:** a entrega é validada contra `https://webhook.site/c1788114-8b5d-4fe3-9aaa-d58eb74e2306`
  (URL fornecida ao avaliador). O `sink` local (httpbin) é o default do compose para ambiente offline;
  basta exportar `SMS_WEBHOOK_URL` para a URL acima ao subir a stack para reproduzir a entrega no
  webhook.site.
- **Janelas temporais das queries:** o dataset tem `transaction_time` de ~semanas atrás, então a Q5
  (reconciliação por dia da venda, últimos 7 dias) pode vir vazia nesse dataset; as demais (baseadas em
  `delivered_at`/`created_at`, que são do processamento) retornam dados normalmente.
