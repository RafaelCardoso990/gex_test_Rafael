# GEX — Esteira de Integração de Webhooks

Backend que recebe webhooks de gateways de pagamento, decripta (quando preciso), valida, persiste,
publica em filas (RabbitMQ) e distribui leads para canais de marketing. Construído em **Python 3.12 +
FastAPI**, **MySQL 8** (SQL puro), **RabbitMQ**, orquestração via **docker-compose**.

---

# Gravação **https://www.loom.com/share/b66516f8ad734242bd10f6f21a07509f**

## 1. Arquitetura (visão de componentes)

```mermaid
flowchart LR
    RP["replay_webhooks.py<br/>(200 payloads)"] -->|"POST /webhooks/{gateway}"| RCV

    subgraph RECEIVER["Receiver — FastAPI"]
        direction TB
        RCV["endpoint"] --> RAW[("raw_payloads<br/>(persiste SEMPRE)")]
        RCV --> DEC{"grummer?<br/>decrypt AES-256-CBC"}
        DEC --> VAL{"schema válido?<br/>(Pydantic)"}
        VAL --> NORM["normaliza<br/>email · phone(E.164) · name"]
        NORM --> IDEM{"idempotente?<br/>idempotency_keys"}
        IDEM --> ROUTE{"roteamento"}
    end

    ROUTE -->|"approved + approved"| Q1[["lead.received"]]
    ROUTE -->|"decrypt falhou"| DLQ1[["lead.dead.decrypt_failed"]]
    ROUTE -->|"schema inválido"| DLQ2[["lead.dead.schema_failed"]]
    ROUTE -->|"status ≠ approved"| DISC["descarta<br/>(só raw_payloads)"]

    Q1 --> CONS["Lead Consumer"]
    CONS -->|"sp_insert_lead (TX)"| DB[("MySQL")]
    CONS -->|"3x falha 1s/4s/16s"| DLQ3[["lead.dead.consumer_failed"]]
    CONS --> QS[["dist.sms"]]
    CONS -.-> QE[["dist.email"]]
    CONS -.-> QC[["dist.callcenter"]]
    CONS -.-> QW[["dist.whatsapp"]]

    QS --> SMS["SMS Distributor"]
    SMS -->|"POST (10% falha)"| WS(("webhook.site"))
    SMS -->|"3x falha"| DLQ4[["dist.dead.sms"]]
    SMS -->|"delivered + lag DB→canal"| DB

    RCV -. metrics .-> PROM[("Prometheus")]
    CONS -. traces (OTel) .-> JAE[("Jaeger")]

    classDef dlq fill:#fde,stroke:#c33;
    class DLQ1,DLQ2,DLQ3,DLQ4 dlq;
```

Canais `email`, `callcenter` e `whatsapp` (linhas tracejadas) têm a fila e a linha em
`distribution_status` criadas, mas só o **SMS** é distribuído de fato (escopo do teste).

---

## 2. Modelo de dados (MER / schema físico)

7 tabelas: as 6 mínimas do enunciado + `idempotency_keys` (gate de idempotência do receiver).

```mermaid
erDiagram
    raw_payloads {
        bigint id PK
        varchar gateway "grummer|lous"
        char correlation_id "UUID4"
        datetime received_at "UTC (ms)"
        json headers
        mediumtext body_raw "original"
        json body_decrypted "null se N/A ou falha"
        enum processing_status "received|decrypt_failed|schema_invalid|duplicate|discarded|routed"
        varchar error_reason "null"
    }
    idempotency_keys {
        bigint id PK
        varchar transaction_id UK "UNIQUE(transaction_id,event)"
        varchar event UK
        char correlation_id
        datetime created_at
    }
    leads {
        bigint id PK
        varchar email UK "UNIQUE"
        varchar first_name "default 'Customer'"
        varchar last_name "null"
        varchar phone_e164 "null"
        tinyint phone_valid
        char country "ISO alpha-2"
        datetime created_at
        datetime updated_at
    }
    orders {
        bigint id PK
        bigint lead_id FK
        varchar gateway
        varchar transaction_id "UNIQUE(gateway,transaction_id)"
        datetime transaction_time "gateway, UTC"
        varchar product_id
        varchar product_name
        varchar product_niche
        int quantity
        decimal amount_usd "DECIMAL(12,2)"
        varchar payment_method
        varchar payment_status
        datetime created_at
    }
    lead_events {
        bigint id PK
        bigint order_id FK
        varchar event "UNIQUE(order_id,event)"
        char correlation_id
        varchar payment_status
        datetime transaction_time "gateway"
        datetime persisted_at "DB UTC"
        int lag_gateway_to_db_seconds
    }
    distribution_status {
        bigint id PK
        bigint order_id FK
        enum channel "SMS|EMAIL|CALL_CENTER|WHATSAPP"
        enum status "pending|delivered|failed|dead"
        char correlation_id
        datetime created_at
        datetime delivered_at "null"
        int lag_db_to_channel_seconds "null"
        int attempts
        varchar last_error "null"
    }
    lead_dead_letter {
        bigint id PK
        varchar source "decrypt|schema|consumer|dist.sms"
        varchar channel "null"
        char correlation_id "null"
        varchar gateway
        varchar transaction_id "null"
        mediumtext payload
        varchar error_reason
        datetime created_at
    }

    leads ||--o{ orders : "1:N tem"
    orders ||--o{ lead_events : "1:N recebe (UNIQUE order+event)"
    orders ||--o{ distribution_status : "1:4 distribui (UNIQUE order+canal)"
```

`idempotency_keys`, `raw_payloads` e `lead_dead_letter` são tabelas de auditoria/controle, sem FK
(propositalmente: precisam sobreviver mesmo quando o payload nunca virou um `order`). DDL completa em
[`sql/001_create_tables.sql`](./sql/001_create_tables.sql); índices em
[`sql/002_indexes.sql`](./sql/002_indexes.sql).

---

## 3. Topologia RabbitMQ

```mermaid
flowchart TB
    subgraph EX["exchange gex.direct (direct)"]
        direction LR
    end

    RCV["Receiver"] -->|"rk: lead.received"| LR_Q[["lead.received"]]
    RCV -->|"rk: dead.decrypt"| D1[["lead.dead.decrypt_failed"]]
    RCV -->|"rk: dead.schema"| D2[["lead.dead.schema_failed"]]

    LR_Q --> C["Lead Consumer"]
    C -->|"retry 1s/4s/16s esgotado"| D3[["lead.dead.consumer_failed"]]
    C -->|"rk: dist.sms"| S1[["dist.sms"]]
    C -->|"rk: dist.email"| S2[["dist.email"]]
    C -->|"rk: dist.callcenter"| S3[["dist.callcenter"]]
    C -->|"rk: dist.whatsapp"| S4[["dist.whatsapp"]]

    S1 --> SD["SMS Distributor"]
    SD -->|"retry esgotado"| D4[["dist.dead.sms"]]

    classDef q fill:#e7f0ff,stroke:#369;
    classDef dlq fill:#fde,stroke:#c33;
    class LR_Q,S1,S2,S3,S4 q;
    class D1,D2,D3,D4 dlq;
```

---

## 4. Sequência — lead end-to-end (happy path)

```mermaid
sequenceDiagram
    autonumber
    participant G as Gateway/Replay
    participant R as Receiver
    participant DB as MySQL
    participant MQ as RabbitMQ
    participant C as Lead Consumer
    participant S as SMS Distributor
    participant W as webhook.site

    G->>R: POST /webhooks/grummer (iv, ciphertext)
    R->>DB: INSERT raw_payloads (correlation_id, UTC)
    R->>R: decrypt AES-256-CBC + parse + validate + normalize
    R->>DB: INSERT idempotency_keys (txn_id, event)  [atômico]
    Note over R,DB: conflito → 200 {"status":"duplicate"}, não publica
    R->>MQ: publish lead.received
    R-->>G: 200 {"status":"accepted"}
    MQ->>C: consume lead.received
    C->>DB: CALL sp_insert_lead(...) [TX: leads+orders+lead_events]
    C->>DB: INSERT 4x distribution_status = pending
    C->>MQ: publish dist.sms | dist.email | dist.callcenter | dist.whatsapp
    MQ->>S: consume dist.sms
    S->>W: POST lead (10% falha simulada)
    W-->>S: 200 OK
    S->>DB: UPDATE distribution_status SMS = delivered + lag DB→canal
```

---

## 5. Sequência — caminho de falha (DLQ)

```mermaid
sequenceDiagram
    autonumber
    participant G as Gateway/Replay
    participant R as Receiver
    participant DB as MySQL
    participant MQ as RabbitMQ

    G->>R: POST /webhooks/grummer (ciphertext corrompido)
    R->>DB: INSERT raw_payloads (body_raw original)
    R->>R: decrypt → ValueError (padding inválido)
    R->>DB: UPDATE raw_payloads.processing_status = 'decrypt_failed'
    R->>MQ: publish lead.dead.decrypt_failed (payload + razão)
    R->>DB: INSERT lead_dead_letter (source='decrypt', error_reason)
    R-->>G: 202 {"status":"dead_letter","reason":"decrypt_failed"}
```

---

## 6. Ciclo de vida do `distribution_status`

```mermaid
stateDiagram-v2
    [*] --> pending: consumer cria 4 canais
    pending --> delivered: POST 200 (sucesso)
    pending --> pending: falha → retry c/ backoff
    pending --> dead: retries esgotados → dist.dead.sms
    delivered --> [*]
    dead --> [*]
```

---

## Como rodar

Pré-requisitos: **Docker** + **docker compose**. Não precisa instalar Python/MySQL na máquina.

> A chave AES (`grummer_secret.txt`) **não está versionada** (secret não vai pro repo). Coloque o
> arquivo fornecido no teste em `docs/grummer_secret.txt` antes de subir.

```bash
# 1. sobe TUDO (MySQL + RabbitMQ + receiver + 2 workers + painéis), sem passo manual
docker compose up -d --build

# 2. injeta os 200 webhooks de exemplo no receiver
#    (precisa de Python + httpx; use uv ou um venv)
uv run python replay_webhooks.py        # ou: RECEIVER_URL=http://localhost:8000 python replay_webhooks.py
```

Painéis para inspecionar:

| Painel | URL | Credenciais |
|---|---|---|
| Adminer (banco) | http://localhost:8080 | servidor `mysql`, user `root`, senha `root`, base `gex` |
| RabbitMQ (filas) | http://localhost:15672 | `guest` / `guest` |
| Receiver (health) | http://localhost:8000/health | — |

> **SMS / webhook.site:** a URL de validação usada na entrega é
> `https://webhook.site/c1788114-8b5d-4fe3-9aaa-d58eb74e2306` (125 entregas ao vivo lá durante a
> validação). Para reproduzir, suba a stack com:
> `SMS_WEBHOOK_URL="https://webhook.site/c1788114-8b5d-4fe3-9aaa-d58eb74e2306" docker compose up -d`.
> Sem essa variável, o distribuidor cai num `sink` local (httpbin) e funciona offline.

## Rodando os testes

```bash
uv sync                 # cria o venv e instala deps (inclui dev)
uv run pytest -q        # 74 testes; integração sobe MySQL/RabbitMQ efêmeros (testcontainers)
```

## Estrutura

```
app/
├── domain/        # lógica pura, testável sem infra (crypto, schema, normalize, routing, pipeline)
├── adapters/      # I/O: db.py (aiomysql), broker.py (aio-pika)
├── receiver/      # FastAPI: app.py (handle_webhook) + main.py (entrypoint uvicorn)
├── workers/       # consumer.py + distributor.py
├── config.py      # settings via env
└── logging.py     # logs JSON + anonimização de PII
sql/               # 001_create_tables, 002_indexes, 003_stored_procs, audit_queries
tests/             # pirâmide: unit (puro) → integração (testcontainers) → e2e (receiver)
docs/              # conceitual_a, conceitual_b, explicativo, db_evidence, defesa
replay_webhooks.py # injeta os 200 webhooks
```

## Reconciliação esperada (gabarito)
| Resultado | Esperado | Obtido |
|---|---|---|
| Total de payloads | 200 | 200 |
| Válidos `approved` únicos em `lead_events` | 125 (±2) | **125** |
| DLQ `decrypt_failed` | ≥ 15 | 15 |
| DLQ `schema_failed` | ≥ 20 | 20 |
| Duplicados pela chave natural (barrados na idempotência) | ≥ 20 | 20 |
| Não-`approved` descartados | 20 | 20 |

Evidência completa do banco: [docs/db_evidence.md](docs/db_evidence.md).

## Documentação

- [docs/explicativo.md](docs/explicativo.md) — visão geral, decisões, premissas, índices, libs
- [docs/conceitual_a.md](docs/conceitual_a.md) — resolução de incidente
- [docs/conceitual_b.md](docs/conceitual_b.md) — decisões de arquitetura (trade-offs)

