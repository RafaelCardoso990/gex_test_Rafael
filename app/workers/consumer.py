"""Lead consumer: consumes lead.received, persists via sp_insert_lead, fans out the 4 channels.

On repeated failure the message goes to lead.dead.consumer_failed with the reason (for reprocessing).
"""
import asyncio
import json

import aio_pika

from app.adapters.broker import Broker
from app.adapters.db import Database
from app.config import Settings
from app.logging import configure_logging, get_logger

BACKOFF = (1, 4, 16)  # 1 attempt + 3 retries with exponential backoff

# distribution routing key -> distribution_status.channel value
_CHANNEL_OF = {
    "dist.sms": "SMS",
    "dist.email": "EMAIL",
    "dist.callcenter": "CALL_CENTER",
    "dist.whatsapp": "WHATSAPP",
}


def _dist_message(message: dict, order_id: int, routing_key: str) -> dict:
    c = message["customer"]
    return {
        "order_id": order_id,
        "channel": _CHANNEL_OF[routing_key],
        "correlation_id": message["correlation_id"],
        "transaction_id": message["transaction_id"],
        "customer": {"first_name": c["first_name"], "phone_e164": c["phone_e164"], "email": c["email"]},
        "product": message["product"]["name"],
        "amount_usd": message["payment"]["amount_usd"],
    }


async def with_retry(factory, *, backoff=BACKOFF, sleep=asyncio.sleep):
    """Run async `factory`, retrying with exponential backoff. Re-raises after the final attempt."""
    for attempt in range(len(backoff) + 1):
        try:
            return await factory()
        except Exception:
            if attempt == len(backoff):
                raise
            await sleep(backoff[attempt])


async def handle_lead(db: Database, broker: Broker, message: dict) -> str:
    """Persist the lead and publish the 4 distribution events. A duplicate re-publishes nothing."""
    result = await db.insert_lead(message)
    if result["status"] == "inserted":
        for routing_key in _CHANNEL_OF:
            await broker.publish(
                routing_key, _dist_message(message, result["order_id"], routing_key),
                message["correlation_id"],
            )
    return result["status"]


async def safe_handle(db: Database, broker: Broker, message: dict, *,
                      backoff=BACKOFF, sleep=asyncio.sleep) -> str:
    """handle_lead with retry; on total failure route to the consumer DLQ (never raises)."""
    try:
        return await with_retry(lambda: handle_lead(db, broker, message), backoff=backoff, sleep=sleep)
    except Exception as exc:
        await broker.publish("lead.dead.consumer_failed",
                             {"payload": message, "reason": str(exc)}, message.get("correlation_id", ""))
        await db.insert_dead_letter(
            source="consumer", correlation_id=message.get("correlation_id"),
            gateway=message.get("gateway"), transaction_id=message.get("transaction_id"),
            payload=json.dumps(message), error_reason=str(exc),
        )
        return "dead_letter"


async def run(settings: Settings) -> None:  # pragma: no cover - process entrypoint
    configure_logging()
    log = get_logger()
    db = await Database.connect(settings)
    broker = await Broker.connect(settings.rabbitmq_url)
    await broker.declare_topology()

    async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):  # always ack; failures are routed to the DLQ
            payload = json.loads(message.body)
            outcome = await safe_handle(db, broker, payload)
            log.info("lead_consumed", correlation_id=payload.get("correlation_id"), outcome=outcome)

    await broker.consume("lead.received", on_message)
    await asyncio.Future()  # run forever


if __name__ == "__main__":  # pragma: no cover
    from app.config import load_settings

    asyncio.run(run(load_settings()))
