"""Integration: MySQL adapter — raw_payloads persistence and race-safe idempotency."""
import asyncio


async def test_insert_raw_payload_returns_id(db):
    rid = await db.insert_raw_payload(
        gateway="lous",
        correlation_id="11111111-1111-4111-8111-111111111111",
        headers={"Content-Type": "application/json"},
        body_raw='{"x": 1}',
        body_decrypted=None,
        processing_status="received",
    )
    assert rid > 0


async def test_reserve_idempotency_first_then_duplicate(db):
    assert await db.reserve_idempotency("ORD-SEQ", "order.approved", "c1") is True
    assert await db.reserve_idempotency("ORD-SEQ", "order.approved", "c2") is False


async def test_reserve_idempotency_is_race_safe(db):
    # 20 identical webhooks arriving at once -> the unique constraint lets exactly one through.
    results = await asyncio.gather(
        *(db.reserve_idempotency("ORD-RACE", "order.approved", f"c{i}") for i in range(20))
    )
    assert sum(results) == 1
