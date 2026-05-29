-- 001_create_tables.sql
-- GEX webhook integration pipeline — schema (MySQL 8, InnoDB, utf8mb4).
-- Integrity constraints (PK / UNIQUE / FK) live here; performance indexes live in 002_indexes.sql.
-- All timestamps are stored in UTC with millisecond precision (DATETIME(3)).

CREATE DATABASE IF NOT EXISTS gex
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE gex;

-- Raw audit log of every reception attempt (including decrypt/schema failures and duplicates).
-- No FK and no UNIQUE on purpose: it must record everything, even payloads that never became an order.
CREATE TABLE raw_payloads (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  gateway           VARCHAR(16)     NOT NULL,
  correlation_id    CHAR(36)        NOT NULL,
  received_at       DATETIME(3)     NOT NULL,
  headers           JSON            NULL,
  body_raw          MEDIUMTEXT      NOT NULL,
  body_decrypted    JSON            NULL,
  processing_status ENUM('received','decrypt_failed','schema_invalid',
                         'duplicate','discarded','routed') NOT NULL DEFAULT 'received',
  error_reason      VARCHAR(500)    NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB;

-- Idempotency gate enforced at the receiver: atomic INSERT on (transaction_id, event).
-- A conflict means the webhook was already received -> respond duplicate, do not republish.
CREATE TABLE idempotency_keys (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  transaction_id VARCHAR(64)     NOT NULL,
  event          VARCHAR(48)     NOT NULL,
  correlation_id CHAR(36)        NOT NULL,
  created_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_idempotency (transaction_id, event)
) ENGINE=InnoDB;

-- Customer base, deduplicated by email. Invalid emails are quarantined upstream and never reach here.
CREATE TABLE leads (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  email       VARCHAR(320)    NOT NULL,
  first_name  VARCHAR(120)    NOT NULL DEFAULT 'Customer',
  last_name   VARCHAR(120)    NULL,
  phone_e164  VARCHAR(20)     NULL,
  phone_valid TINYINT(1)      NOT NULL DEFAULT 0,
  country     CHAR(2)         NULL,
  created_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                              ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_email (email)
) ENGINE=InnoDB;

-- One row per transaction (immutable order facts). The same order may receive many events over time.
CREATE TABLE orders (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  lead_id          BIGINT UNSIGNED NOT NULL,
  gateway          VARCHAR(16)     NOT NULL,
  transaction_id   VARCHAR(64)     NOT NULL,
  transaction_time DATETIME(3)     NOT NULL,
  product_id       VARCHAR(48)     NULL,
  product_name     VARCHAR(120)    NULL,
  product_niche    VARCHAR(48)     NULL,
  quantity         INT UNSIGNED    NULL,
  amount_usd       DECIMAL(12,2)   NULL,
  payment_method   VARCHAR(24)     NULL,
  payment_status   VARCHAR(16)     NULL,
  created_at       DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_gateway_txn (gateway, transaction_id),
  CONSTRAINT fk_orders_lead FOREIGN KEY (lead_id) REFERENCES leads (id)
) ENGINE=InnoDB;

-- One row per (order, event). UNIQUE(order_id, event) is the relational reflection of the
-- idempotency key and protects against at-least-once redelivery from the queue.
CREATE TABLE lead_events (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id                  BIGINT UNSIGNED NOT NULL,
  event                     VARCHAR(48)     NOT NULL,
  correlation_id            CHAR(36)        NOT NULL,
  payment_status            VARCHAR(16)     NULL,
  transaction_time          DATETIME(3)     NOT NULL,
  persisted_at              DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  lag_gateway_to_db_seconds INT             NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_order_event (order_id, event),
  CONSTRAINT fk_events_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB;

-- One row per (order, channel). Materialized status so audit queries read directly from the table.
CREATE TABLE distribution_status (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id                  BIGINT UNSIGNED NOT NULL,
  channel                   ENUM('SMS','EMAIL','CALL_CENTER','WHATSAPP') NOT NULL,
  status                    ENUM('pending','delivered','failed','dead') NOT NULL DEFAULT 'pending',
  correlation_id            CHAR(36)        NOT NULL,
  created_at                DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  delivered_at              DATETIME(3)     NULL,
  lag_db_to_channel_seconds INT             NULL,
  attempts                  INT UNSIGNED    NOT NULL DEFAULT 0,
  last_error                VARCHAR(500)    NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_order_channel (order_id, channel),
  CONSTRAINT fk_dist_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB;

-- Dead-letter store for reprocessing. No FK: the failing payload may have no order yet.
CREATE TABLE lead_dead_letter (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source         VARCHAR(32)     NOT NULL,
  channel        VARCHAR(16)     NULL,
  correlation_id CHAR(36)        NULL,
  gateway        VARCHAR(16)     NULL,
  transaction_id VARCHAR(64)     NULL,
  payload        MEDIUMTEXT      NOT NULL,
  error_reason   VARCHAR(1000)   NOT NULL,
  created_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id)
) ENGINE=InnoDB;
