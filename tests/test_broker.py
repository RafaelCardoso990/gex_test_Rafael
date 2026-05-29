"""Integration: RabbitMQ adapter — topology declaration and publishing."""
import json

import aio_pika


async def _get_one(url: str, queue_name: str) -> tuple[dict, str | None]:
    """Consume and ack one message, returning (body, correlation_id). Acks before closing."""
    conn = await aio_pika.connect_robust(url)
    try:
        channel = await conn.channel()
        queue = await channel.get_queue(queue_name)
        message = await queue.get(timeout=5)
        await message.ack()
        return json.loads(message.body), message.correlation_id
    finally:
        await conn.close()


async def test_declare_topology_is_idempotent(broker):
    await broker.declare_topology()  # second call must not raise


async def test_publish_delivers_message_with_correlation_id(broker, rabbitmq_url):
    await broker.publish("lead.received", {"transaction_id": "ORD-1"}, "corr-123")
    body, correlation_id = await _get_one(rabbitmq_url, "lead.received")
    assert body == {"transaction_id": "ORD-1"}
    assert correlation_id == "corr-123"


async def test_publish_routes_by_routing_key(broker, rabbitmq_url):
    await broker.publish("dist.sms", {"order_id": 7}, "corr-sms")
    body, _ = await _get_one(rabbitmq_url, "dist.sms")
    assert body == {"order_id": 7}
