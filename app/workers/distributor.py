"""SMS distributor: consumes dist.sms, POSTs to the channel (webhook.site), updates status.

Simulates a 10% provider failure (seeded RNG so it is deterministic in tests). On repeated failure
the message goes to dist.dead.sms; on success the SMS row is marked delivered with the DB->channel lag.
"""
import asyncio
import json
import random

import aio_pika
import httpx

from app.adapters.broker import Broker
from app.adapters.db import Database
from app.config import Settings
from app.logging import configure_logging, get_logger
from app.workers.consumer import with_retry  # shared retry/backoff helper

BACKOFF = (1, 4, 16)
FAIL_RATE = 0.10


class SmsSendError(Exception):
    """Raised when the SMS channel send fails (simulated or real)."""


async def send_sms(http: httpx.AsyncClient, url: str, payload: dict, *,
                   rng: random.Random, fail_rate: float = FAIL_RATE) -> None:
    if rng.random() < fail_rate:
        raise SmsSendError("simulated provider failure")
    response = await http.post(url, json=payload)
    if response.status_code >= 400:
        raise SmsSendError(f"channel returned HTTP {response.status_code}")


async def deliver(db: Database, broker: Broker, http: httpx.AsyncClient, url: str, message: dict, *,
                  rng: random.Random, fail_rate: float = FAIL_RATE,
                  backoff=BACKOFF, sleep=asyncio.sleep) -> str:
    """Try to deliver one SMS with retry. Returns 'delivered' or 'dead'."""
    order_id = message["order_id"]
    try:
        await with_retry(lambda: send_sms(http, url, message, rng=rng, fail_rate=fail_rate),
                         backoff=backoff, sleep=sleep)
    except Exception as exc:
        await db.mark_sms_dead(order_id, str(exc))
        await broker.publish("dist.dead.sms", {"payload": message, "reason": str(exc)},
                             message.get("correlation_id", ""))
        await db.insert_dead_letter(
            source="dist.sms", channel="SMS", correlation_id=message.get("correlation_id"),
            gateway=None, transaction_id=message.get("transaction_id"),
            payload=json.dumps(message), error_reason=str(exc),
        )
        return "dead"
    await db.mark_sms_delivered(order_id)
    return "delivered"


async def run(settings: Settings) -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    log = get_logger()
    db = await Database.connect(settings)
    broker = await Broker.connect(settings.rabbitmq_url)
    await broker.declare_topology()
    rng = random.Random()

    async with httpx.AsyncClient(timeout=10) as http:
        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            async with message.process(requeue=False):
                payload = json.loads(message.body)
                outcome = await deliver(db, broker, http, settings.sms_webhook_url, payload, rng=rng)
                log.info("sms_distributed", correlation_id=payload.get("correlation_id"),
                         order_id=payload.get("order_id"), outcome=outcome)

        await broker.consume("dist.sms", on_message)
        await asyncio.Future()  # run forever


if __name__ == "__main__":  # pragma: no cover
    from app.config import load_settings

    asyncio.run(run(load_settings()))
