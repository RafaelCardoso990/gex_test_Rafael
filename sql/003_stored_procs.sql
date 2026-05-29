-- 003_stored_procs.sql
-- sp_insert_lead: atomically persists lead + order + operational event (and the 4 pending
-- distribution channels) in a single transaction. Called by the lead consumer.
-- Idempotent: re-delivery of the same (order, event) is a no-op, not an error.

USE gex;

DROP PROCEDURE IF EXISTS sp_insert_lead;

DELIMITER $$

CREATE PROCEDURE sp_insert_lead(
  IN  p_email            VARCHAR(320),
  IN  p_first_name       VARCHAR(120),
  IN  p_last_name        VARCHAR(120),
  IN  p_phone_e164       VARCHAR(20),
  IN  p_phone_valid      TINYINT,
  IN  p_country          CHAR(2),
  IN  p_gateway          VARCHAR(16),
  IN  p_transaction_id   VARCHAR(64),
  IN  p_transaction_time DATETIME(3),
  IN  p_product_id       VARCHAR(48),
  IN  p_product_name     VARCHAR(120),
  IN  p_product_niche    VARCHAR(48),
  IN  p_quantity         INT UNSIGNED,
  IN  p_amount_usd       DECIMAL(12,2),
  IN  p_payment_method   VARCHAR(24),
  IN  p_payment_status   VARCHAR(16),
  IN  p_event            VARCHAR(48),
  IN  p_correlation_id   CHAR(36),
  OUT p_lead_id          BIGINT UNSIGNED,
  OUT p_order_id         BIGINT UNSIGNED,
  OUT p_event_id         BIGINT UNSIGNED,
  OUT p_status           VARCHAR(16)        -- 'inserted' | 'duplicate'
)
proc: BEGIN
  DECLARE v_now       DATETIME(3);
  DECLARE v_dup_event TINYINT DEFAULT 0;

  -- Any unexpected error: roll back the whole unit and propagate to the caller.
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  -- Duplicate (order_id, event) from at-least-once redelivery: treat as idempotent no-op.
  DECLARE CONTINUE HANDLER FOR 1062 SET v_dup_event = 1;

  SET v_now = UTC_TIMESTAMP(3);

  START TRANSACTION;

  -- Find-or-create the lead by email (existing customer keeps its profile; we just refresh updated_at).
  INSERT INTO leads (email, first_name, last_name, phone_e164, phone_valid, country)
  VALUES (p_email, COALESCE(NULLIF(p_first_name, ''), 'Customer'),
          p_last_name, p_phone_e164, p_phone_valid, p_country)
  ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), updated_at = v_now;
  SET p_lead_id = LAST_INSERT_ID();

  -- Find-or-create the order by (gateway, transaction_id).
  INSERT INTO orders (lead_id, gateway, transaction_id, transaction_time, product_id, product_name,
                      product_niche, quantity, amount_usd, payment_method, payment_status)
  VALUES (p_lead_id, p_gateway, p_transaction_id, p_transaction_time, p_product_id, p_product_name,
          p_product_niche, p_quantity, p_amount_usd, p_payment_method, p_payment_status)
  ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id);
  SET p_order_id = LAST_INSERT_ID();

  -- Operational event with the gateway->DB lag in seconds. UNIQUE(order_id, event) enforces idempotency.
  INSERT INTO lead_events (order_id, event, correlation_id, payment_status, transaction_time,
                           persisted_at, lag_gateway_to_db_seconds)
  VALUES (p_order_id, p_event, p_correlation_id, p_payment_status, p_transaction_time,
          v_now, TIMESTAMPDIFF(SECOND, p_transaction_time, v_now));

  IF v_dup_event = 1 THEN
    SET p_event_id = NULL;
    SET p_status   = 'duplicate';
    COMMIT;                 -- lead/order upserts are idempotent; release locks and exit cleanly
    LEAVE proc;
  END IF;

  SET p_event_id = LAST_INSERT_ID();

  -- Pre-create the 4 channels as pending in the same transaction (no partial state).
  INSERT IGNORE INTO distribution_status (order_id, channel, status, correlation_id)
  VALUES (p_order_id, 'SMS',         'pending', p_correlation_id),
         (p_order_id, 'EMAIL',       'pending', p_correlation_id),
         (p_order_id, 'CALL_CENTER', 'pending', p_correlation_id),
         (p_order_id, 'WHATSAPP',    'pending', p_correlation_id);

  SET p_status = 'inserted';
  COMMIT;
END proc $$

DELIMITER ;
