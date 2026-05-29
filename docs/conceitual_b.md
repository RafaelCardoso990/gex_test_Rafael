# Conceitual — Parte B: Decisões de arquitetura

## 1. Idempotência: por que `transaction_id + event` e não só `transaction_id`?
Um mesmo pedido recebe **vários eventos ao longo do tempo**: `order.approved` hoje, `order.refunded`
ou `order.declined` amanhã. A chave composta permite registrar **cada transição uma única vez** sem
bloquear as legítimas.

- **Só `transaction_id`** falha quando há múltiplos eventos por pedido: o `order.approved` entra, mas o
  `order.refunded` do dia seguinte seria rejeitado como "duplicata" → o cliente reembolsado continuaria
  recebendo ligação do call center. Perda de uma atualização crítica de estado.
- **Só `event`** colidiria entre pedidos diferentes (todos têm `order.approved`) → catastrófico, só o
  primeiro pedido entraria.
- **`transaction_id + event`** falha apenas se o gateway reenviar o **mesmo evento com conteúdo
  diferente** (ex.: correção de valor no mesmo `order.approved`) — a 2ª versão seria barrada. Como na
  GEX um evento de venda é um **fato imutável**, esse trade-off é aceitável; se atualizações in-place
  fossem esperadas, eu acrescentaria um hash do payload à chave.

No projeto isso vira **duas camadas**: `idempotency_keys` no receiver (responde `duplicate` na hora e
segura a race) e `lead_events` UNIQUE no consumer (blinda contra a fila republicar, pois RabbitMQ é
*at-least-once*).

## 2. Cripto: AES-256-CBC vs AES-256-GCM (para um webhook novo, não-grummer)
Escolheria **GCM**. CBC apenas **cifra**; GCM é **AEAD** (cifra **e autentica** com uma tag).

Ataques a que o CBC é vulnerável e o GCM não:
- **Maleabilidade / bit-flipping:** alterar bytes do ciphertext muda o plaintext de forma previsível,
  sem detecção (CBC não tem integridade).
- **Padding oracle (estilo POODLE):** se o servidor vaza erro de padding, dá pra decifrar byte a byte.
- **Adulteração silenciosa:** CBC não detecta que a mensagem foi modificada.

GCM resolve com a **tag de autenticação**: qualquer alteração faz a verificação falhar.
**Trade-off:** GCM exige **nonce único por chave** — reuso de nonce é catastrófico (vaza a chave de
autenticação), então exige gestão disciplinada. CBC só exige IV imprevisível. Para o `grummer` (legado
em CBC), eu não trocaria o algoritmo de imediato: mitigaria com **encrypt-then-HMAC** para ganhar
integridade sem quebrar compatibilidade.

## 3. Backpressure: SMS com 90% de erro — como proteger o resto?
**Por que RabbitMQ + retry exponencial sozinho não basta:** com 90% de erro, quase tudo re-enfileira e
re-tenta → a fila `dist.sms` incha, os workers ficam presos dormindo nos backoffs e o throughput
despenca. É um **retry storm**: o retry *amplifica* a carga sobre um provedor já caído. No nosso
desenho, o retry é in-process (trade-off documentado), então os workers bloqueariam — agravando.

Proteções:
- **Circuit breaker:** ao detectar taxa de erro alta, "abre" e passa a **falhar rápido** em vez de
  martelar o provedor; meia-abertura testa a recuperação. Para o retry storm.
- **Bulkhead / isolamento:** limitar a concorrência do distribuidor SMS para que o canal quebrado não
  consuma todos os recursos e derrube email/whatsapp/call center.
- **DLQ rápida + replay controlado:** mandar pra `dist.dead.sms` cedo (sem mil retries) e reprocessar
  em lote quando o provedor voltar.
- **Backoff com jitter:** evita *thundering herd* na recuperação.

**Trade-off:** o circuit breaker pode abrir em falso positivo (blip do provedor) e atrasar entregas
legítimas; é questão de calibrar thresholds. Mas protege o sistema inteiro de um canal doente.

## 4. Migração entre linguagens (receiver+decrypt: Python ↔ Go) — no contexto da GEX
**3 sinais de que VALE migrar (para Go):**
1. **Receiver virou gargalo de CPU:** com o crescimento da GEX (picos de campanha levando de milhares/dia
   a dezenas de milhares/min), o decrypt AES + parsing satura CPU e o overhead do Python passa a limitar
   throughput por instância, inflando custo de infra.
2. **Latência de cauda (p99) inaceitável:** o gateway tem timeout curto no webhook e começamos a perder
   webhooks sob carga; Go (goroutines, sem GIL) daria latência mais previsível.
3. **Padronização da plataforma:** se os serviços críticos vizinhos já são Go, manter o receiver em
   Python fragmenta observabilidade/operação e aumenta custo de manutenção.

**3 sinais de que NÃO vale:**
1. **O gargalo é I/O, não CPU** (caso atual): o tempo é gasto em banco/fila/provedores; o async em
   Python segura bem e migrar não move a agulha.
2. **Risco vs ganho:** o receiver é o componente que "recebe dinheiro" e está testado e estável;
   reescrever introduz risco de regressão sem ganho proporcional.
3. **Velocidade de evolução:** o time domina Python, o ecossistema (pydantic/cryptography) é maduro e a
   esteira muda rápido (novos gateways, novas regras de roteamento) — Go desaceleraria o negócio.
