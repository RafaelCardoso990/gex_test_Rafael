-- 002_indexes.sql
-- Performance indexes for the audit queries (see sql/audit_queries.sql).
-- Each index is justified by the access pattern of a specific query.
-- Note: on the 200-row sample dataset the optimizer may full-scan (tables are tiny and still < 1s);
-- these indexes are sized for production volume (thousands of leads/day).

USE gex;

-- Q1 (avg lag gateway -> SMS delivered, last 24h): filters channel='SMS' AND status='delivered'
-- AND delivered_at >= now-24h. Also narrows by channel for Q3 (SMS success rate).
CREATE INDEX ix_dist_channel_status_delivered
  ON distribution_status (channel, status, delivered_at);

-- Q2 (pending older than 5 min): filters status='pending' AND created_at < now-5min, any channel.
CREATE INDEX ix_dist_status_created
  ON distribution_status (status, created_at);

-- Q5 (reconciliation: approved per day, last 7d): filters event='order.approved'
-- AND persisted_at >= now-7d, grouped by day.
CREATE INDEX ix_events_event_persisted
  ON lead_events (event, persisted_at);

-- Q4 (DLQ count by reason, last 24h): filters created_at >= now-24h, grouped by source.
CREATE INDEX ix_dld_source_created
  ON lead_dead_letter (source, created_at);

-- Audit / incident investigation (Part A): locate raw payloads by gateway within a time window.
CREATE INDEX ix_raw_gateway_received
  ON raw_payloads (gateway, received_at);
