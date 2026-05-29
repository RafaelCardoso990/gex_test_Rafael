"""RabbitMQ adapter (aio-pika). Declares the topology and publishes; no business logic here."""
import json

import aio_pika

EXCHANGE = "gex.direct"

# Every queue is bound to the direct exchange with routing_key == queue name.
QUEUES = (
    "lead.received",
    "lead.dead.decrypt_failed",
    "lead.dead.schema_failed",
    "lead.dead.consumer_failed",
    "dist.sms",
    "dist.email",
    "dist.callcenter",
    "dist.whatsapp",
    "dist.dead.sms",
)


class Broker:
    def __init__(self, connection: aio_pika.abc.AbstractRobustConnection,
                 channel: aio_pika.abc.AbstractChannel,
                 exchange: aio_pika.abc.AbstractExchange):
        self._connection = connection
        self._channel = channel
        self._exchange = exchange

    @classmethod
    async def connect(cls, url: str) -> "Broker":
        connection = await aio_pika.connect_robust(url)
        channel = await connection.channel()
        exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
        return cls(connection, channel, exchange)

    async def declare_topology(self) -> None:
        """Create all queues and bind them to the exchange. Idempotent (safe to call on startup)."""
        for name in QUEUES:
            queue = await self._channel.declare_queue(name, durable=True)
            await queue.bind(self._exchange, routing_key=name)

    async def publish(self, routing_key: str, payload: dict, correlation_id: str) -> None:
        message = aio_pika.Message(
            body=json.dumps(payload).encode(),
            correlation_id=correlation_id,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await self._exchange.publish(message, routing_key=routing_key)

    async def consume(self, queue_name: str, handler) -> None:
        """Start consuming a queue; `handler` receives each aio_pika IncomingMessage."""
        queue = await self._channel.get_queue(queue_name)
        await queue.consume(handler)

    async def purge_all(self) -> None:
        """Empty every queue (operational/test utility — start from a clean slate)."""
        for name in QUEUES:
            queue = await self._channel.get_queue(name)
            await queue.purge()

    async def close(self) -> None:
        await self._connection.close()
