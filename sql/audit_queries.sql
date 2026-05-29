-- audit_queries.sql
-- Operational/audit queries. All time windows use UTC_TIMESTAMP(3) because data is stored in UTC.
-- Each query notes the index it relies on (see 002_indexes.sql). EXPLAIN output is validated
-- against the populated dataset (Day 5) and documented in docs/.

USE gex;

-- ---------------------------------------------------------------------------------------------
-- Q1: Average lag (seconds) between gateway transaction_time and SMS delivered_at, per gateway,
--     over the last 24h.
-- Index: ix_dist_channel_status_delivered (channel, status, delivered_at).
-- ---------------------------------------------------------------------------------------------
SELECT o.gateway,
       COUNT(*)                                                       AS delivered_count,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, o.transaction_time, ds.delivered_at)), 1) AS avg_lag_seconds
FROM distribution_status ds
JOIN orders o ON o.id = ds.order_id
WHERE ds.channel = 'SMS'
  AND ds.status  = 'delivered'
  AND ds.delivered_at >= UTC_TIMESTAMP(3) - INTERVAL 24 HOUR
GROUP BY o.gateway;

-- ---------------------------------------------------------------------------------------------
-- Q2: Channels stuck in 'pending' for more than 5 minutes. Lists order_id, channel and age (s).
-- Index: ix_dist_status_created (status, created_at).
-- ---------------------------------------------------------------------------------------------
SELECT ds.order_id,
       ds.channel,
       TIMESTAMPDIFF(SECOND, ds.created_at, UTC_TIMESTAMP(3)) AS pending_age_seconds
FROM distribution_status ds
WHERE ds.status = 'pending'
  AND ds.created_at < UTC_TIMESTAMP(3) - INTERVAL 5 MINUTE
ORDER BY pending_age_seconds DESC;

-- ---------------------------------------------------------------------------------------------
-- Q3: SMS success rate per product, per hour, over the last 6h.
-- success = delivered / total (total = every SMS row created in the window).
-- Index: ix_dist_channel_status_delivered narrows by channel; created_at range applied on the set.
-- ---------------------------------------------------------------------------------------------
SELECT o.product_name,
       DATE_FORMAT(ds.created_at, '%Y-%m-%d %H:00')              AS hour_bucket,
       COUNT(*)                                                  AS total,
       SUM(ds.status = 'delivered')                              AS delivered,
       ROUND(100 * SUM(ds.status = 'delivered') / COUNT(*), 2)   AS success_rate_pct
FROM distribution_status ds
JOIN orders o ON o.id = ds.order_id
WHERE ds.channel = 'SMS'
  AND ds.created_at >= UTC_TIMESTAMP(3) - INTERVAL 6 HOUR
GROUP BY o.product_name, hour_bucket
ORDER BY hour_bucket, o.product_name;

-- ---------------------------------------------------------------------------------------------
-- Q4: Dead-letter count by reason, over the last 24h.
-- Index: ix_dld_source_created (source, created_at).
-- ---------------------------------------------------------------------------------------------
SELECT source,
       COUNT(*) AS dead_count
FROM lead_dead_letter
WHERE created_at >= UTC_TIMESTAMP(3) - INTERVAL 24 HOUR
GROUP BY source
ORDER BY dead_count DESC;

-- ---------------------------------------------------------------------------------------------
-- Q5: Reconciliation — approved leads (lead_events) vs SMS delivered, per business day (gateway
--     transaction_time), last 7 days, with absolute gap and delivered percentage.
-- Index: ix_events_event_persisted helps the event filter; orders joined by PK.
-- ---------------------------------------------------------------------------------------------
WITH approved AS (
    SELECT DATE(o.transaction_time) AS day, COUNT(*) AS approved_count
    FROM lead_events le
    JOIN orders o ON o.id = le.order_id
    WHERE le.event = 'order.approved'
      AND o.transaction_time >= UTC_TIMESTAMP(3) - INTERVAL 7 DAY
    GROUP BY DATE(o.transaction_time)
),
delivered AS (
    SELECT DATE(o.transaction_time) AS day, COUNT(*) AS delivered_sms_count
    FROM distribution_status ds
    JOIN orders o ON o.id = ds.order_id
    WHERE ds.channel = 'SMS'
      AND ds.status  = 'delivered'
      AND o.transaction_time >= UTC_TIMESTAMP(3) - INTERVAL 7 DAY
    GROUP BY DATE(o.transaction_time)
)
SELECT a.day,
       a.approved_count                                                       AS approved,
       COALESCE(d.delivered_sms_count, 0)                                     AS delivered_sms,
       a.approved_count - COALESCE(d.delivered_sms_count, 0)                  AS gap_abs,
       ROUND(100 * COALESCE(d.delivered_sms_count, 0) / a.approved_count, 2)  AS delivered_pct
FROM approved a
LEFT JOIN delivered d ON d.day = a.day
ORDER BY a.day;
