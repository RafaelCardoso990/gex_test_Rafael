"""Consumer: retry/backoff (pure) and lead persistence + fan-out (integration)."""
import pytest

from app.workers.consumer import handle_lead, safe_handle, with_retry

LEAD_MSG = {
    "correlation_id": "11111111-1111-4111-8111-111111111111",
    "gateway": "lous",
    "transaction_id": "ORD-CONS-1",
    "transaction_time": "2026-05-01T22:24:00.275205+00:00",
    "event": "order.approved",
    "customer": {
        "email": "ann@example.com", "first_name": "Ann", "last_name": "Lee",
        "phone_e164": "+16502530000", "phone_valid": True, "country": "US",
    },
    "product": {"id": "PROD-1", "name": "Fit Burn", "niche": "weight_loss", "quantity": 2},
    "payment": {"status": "approved", "amount_usd": 100.0, "method": "paypal"},
}

DIST_QUEUES = ("dist.sms", "dist.email", "dist.callcenter", "dist.whatsapp")


# --- pure retry logic (no infra) ---

async def test_with_retry_succeeds_after_transient_failures():
    calls, slept = [], []

    async def factory():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("transient")
        return "ok"

    async def sleep(seconds):
        slept.append(seconds)

    assert await with_retry(factory, backoff=(1, 4, 16), sleep=sleep) == "ok"
    assert len(calls) == 3
    assert slept == [1, 4]  # two backoffs before the third (successful) attempt


async def test_with_retry_reraises_after_exhausting():
    slept = []

    async def factory():
        raise RuntimeError("always")

    async def sleep(seconds):
        slept.append(seconds)

    with pytest.raises(RuntimeError, match="always"):
        await with_retry(factory, backoff=(1, 4, 16), sleep=sleep)
    assert slept == [1, 4, 16]  # slept before each retry, then gave up


# --- persistence + fan-out (real MySQL + RabbitMQ) ---

async def test_handle_lead_persists_and_fans_out(db, broker, drain, query):
    assert await handle_lead(db, broker, LEAD_MSG) == "inserted"
    assert (await query("SELECT COUNT(*) FROM leads"))[0][0] == 1
    assert (await query("SELECT COUNT(*) FROM orders"))[0][0] == 1
    assert (await query("SELECT COUNT(*) FROM lead_events"))[0][0] == 1
    assert (await query("SELECT COUNT(*) FROM distribution_status WHERE status='pending'"))[0][0] == 4
    for queue_name in DIST_QUEUES:
        assert len(await drain(queue_name)) == 1


async def test_handle_lead_duplicate_does_not_republish(db, broker, drain):
    await handle_lead(db, broker, LEAD_MSG)
    assert await handle_lead(db, broker, LEAD_MSG) == "duplicate"
    assert len(await drain("dist.sms")) == 1  # published only on the first (new) insert


async def test_safe_handle_routes_failure_to_consumer_dlq(db, broker, drain, query):
    broken = {k: v for k, v in LEAD_MSG.items() if k != "customer"}  # missing customer -> KeyError

    async def no_sleep(_seconds):
        pass

    outcome = await safe_handle(db, broker, broken, backoff=(0, 0, 0), sleep=no_sleep)
    assert outcome == "dead_letter"
    assert len(await drain("lead.dead.consumer_failed")) == 1
    assert (await query("SELECT source FROM lead_dead_letter"))[0][0] == "consumer"
