"""MySQL adapter (aiomysql). Thin I/O layer around the schema; no business logic here."""
import asyncio
import json
from datetime import datetime, timezone

import aiomysql
from pymysql.err import IntegrityError

from app.config import Settings


def _to_naive_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a naive UTC datetime (the DB stores everything in UTC)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class Database:
    def __init__(self, pool: aiomysql.Pool):
        self._pool = pool

    @classmethod
    async def connect(cls, settings: Settings, *, attempts: int = 15, delay: float = 2.0) -> "Database":
        """Create the pool, retrying while the database comes up (resilient container startup)."""
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                pool = await aiomysql.create_pool(
                    host=settings.mysql_host,
                    port=settings.mysql_port,
                    user=settings.mysql_user,
                    password=settings.mysql_password,
                    db=settings.mysql_db,
                    autocommit=True,
                    minsize=1,
                    maxsize=10,
                )
                return cls(pool)
            except Exception as exc:  # not ready yet
                last_error = exc
                await asyncio.sleep(delay)
        raise last_error

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    async def insert_raw_payload(
        self,
        *,
        gateway: str,
        correlation_id: str,
        headers: dict | None,
        body_raw: str,
        body_decrypted: dict | None,
        processing_status: str,
        error_reason: str | None = None,
    ) -> int:
        """Persist the raw reception (always, even on decrypt/schema failure). Returns the row id."""
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO raw_payloads "
                "(gateway, correlation_id, received_at, headers, body_raw, body_decrypted, "
                " processing_status, error_reason) "
                "VALUES (%s, %s, UTC_TIMESTAMP(3), %s, %s, %s, %s, %s)",
                (
                    gateway,
                    correlation_id,
                    json.dumps(headers) if headers is not None else None,
                    body_raw,
                    json.dumps(body_decrypted) if body_decrypted is not None else None,
                    processing_status,
                    error_reason,
                ),
            )
            return cur.lastrowid

    async def finalize_raw_payload(
        self, raw_id: int, *, status: str, body_decrypted: dict | None = None,
        error_reason: str | None = None,
    ) -> None:
        """Update the raw row once the outcome is known (status + decrypted body + failure reason)."""
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE raw_payloads SET processing_status = %s, body_decrypted = %s, "
                "error_reason = %s WHERE id = %s",
                (
                    status,
                    json.dumps(body_decrypted) if body_decrypted is not None else None,
                    error_reason,
                    raw_id,
                ),
            )

    async def insert_dead_letter(
        self, *, source: str, correlation_id: str | None, gateway: str | None,
        transaction_id: str | None, payload: str, error_reason: str, channel: str | None = None,
    ) -> int:
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO lead_dead_letter "
                "(source, channel, correlation_id, gateway, transaction_id, payload, error_reason, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(3))",
                (source, channel, correlation_id, gateway, transaction_id, payload, error_reason),
            )
            return cur.lastrowid

    async def reserve_idempotency(self, transaction_id: str, event: str, correlation_id: str) -> bool:
        """Atomically claim (transaction_id, event). True if first-seen, False if duplicate.

        Race-safe by design: relies on the UNIQUE constraint instead of a SELECT-then-INSERT, so two
        identical webhooks arriving concurrently resolve to exactly one winner.
        """
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO idempotency_keys (transaction_id, event, correlation_id) "
                    "VALUES (%s, %s, %s)",
                    (transaction_id, event, correlation_id),
                )
                return True
            except IntegrityError:
                return False

    async def insert_lead(self, message: dict) -> dict:
        """Persist lead + order + event + 4 pending channels atomically via the stored procedure.

        Returns {"order_id", "status"} where status is 'inserted' or 'duplicate'.
        """
        c, p, pay = message["customer"], message["product"], message["payment"]
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "CALL sp_insert_lead(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "@lead_id,@order_id,@event_id,@status)",
                (
                    c["email"], c["first_name"], c["last_name"], c["phone_e164"],
                    int(c["phone_valid"]), c["country"],
                    message["gateway"], message["transaction_id"],
                    _to_naive_utc(message["transaction_time"]),
                    p["id"], p["name"], p["niche"], p["quantity"],
                    pay["amount_usd"], pay["method"], pay["status"],
                    message["event"], message["correlation_id"],
                ),
            )
            await cur.execute("SELECT @order_id, @status")
            order_id, status = await cur.fetchone()
            return {"order_id": order_id, "status": status}

    async def mark_sms_delivered(self, order_id: int) -> None:
        """Mark the SMS channel delivered and compute the DB->channel lag in seconds."""
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE distribution_status SET status = 'delivered', delivered_at = UTC_TIMESTAMP(3), "
                "lag_db_to_channel_seconds = TIMESTAMPDIFF(SECOND, created_at, UTC_TIMESTAMP(3)), "
                "attempts = attempts + 1 WHERE order_id = %s AND channel = 'SMS'",
                (order_id,),
            )

    async def mark_sms_dead(self, order_id: int, error: str) -> None:
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE distribution_status SET status = 'dead', attempts = attempts + 1, "
                "last_error = %s WHERE order_id = %s AND channel = 'SMS'",
                (error, order_id),
            )
