"""FastAPI receiver: POST /webhooks/{gateway}. Orchestrates the pure pipeline around the I/O adapters.

raw_payloads is written first (audit-first), then the pipeline decides the outcome, then we route to a
queue / DLQ and finalize the raw row. `handle_webhook` holds the logic and is reused by create_app
(tests, injected deps) and by main.py (production, deps on app.state).
"""
import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.adapters.broker import Broker
from app.adapters.db import Database
from app.domain.pipeline import Outcome, ProcessResult, process
from app.logging import anonymize, get_logger

GATEWAYS = {"grummer", "lous"}
_log = get_logger()

# outcome -> (raw status, DLQ routing key, dead_letter source)
_DLQ = {
    Outcome.DECRYPT_FAILED: ("decrypt_failed", "lead.dead.decrypt_failed", "decrypt"),
    Outcome.SCHEMA_FAILED: ("schema_invalid", "lead.dead.schema_failed", "schema"),
    Outcome.QUARANTINED: ("schema_invalid", "lead.dead.schema_failed", "schema"),
}


def _lead_message(result: ProcessResult, gateway: str, correlation_id: str) -> dict:
    """Flat payload the consumer feeds into sp_insert_lead."""
    e, c = result.event, result.customer
    return {
        "correlation_id": correlation_id,
        "gateway": gateway,
        "transaction_id": e.transaction_id,
        "transaction_time": e.transaction_time.isoformat(),
        "event": e.event,
        "customer": {
            "email": c.email, "first_name": c.first_name, "last_name": c.last_name,
            "phone_e164": c.phone_e164, "phone_valid": c.phone_valid, "country": c.country,
        },
        "product": {
            "id": e.product.id, "name": e.product.name,
            "niche": e.product.niche, "quantity": e.product.quantity,
        },
        "payment": {
            "status": e.payment.status, "amount_usd": e.payment.amount_usd, "method": e.payment.method,
        },
    }


async def handle_webhook(db: Database, broker: Broker, key: bytes, gateway: str, request: Request):
    if gateway not in GATEWAYS:
        return JSONResponse({"status": "unknown_gateway"}, status_code=404)

    started = time.perf_counter()
    correlation_id = str(uuid.uuid4())
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", "replace")
    raw_id = await db.insert_raw_payload(
        gateway=gateway, correlation_id=correlation_id, headers=dict(request.headers),
        body_raw=body_text, body_decrypted=None, processing_status="received",
    )

    def done(outcome: str, status_code: int, payload: dict, email: str | None = None):
        _log.info(
            "webhook_processed", correlation_id=correlation_id, gateway=gateway,
            webhook_event=payload.get("event"), outcome=outcome, customer=anonymize(email),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return JSONResponse(payload, status_code=status_code)

    # Body must be a JSON object; anything else is treated as a schema failure.
    try:
        body = json.loads(body_bytes)
        if not isinstance(body, dict):
            raise ValueError("body is not a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        await db.finalize_raw_payload(raw_id, status="schema_invalid", error_reason=str(exc))
        await broker.publish("lead.dead.schema_failed",
                             {"gateway": gateway, "payload": body_text, "reason": str(exc)}, correlation_id)
        await db.insert_dead_letter(source="schema", correlation_id=correlation_id, gateway=gateway,
                                    transaction_id=None, payload=body_text, error_reason=str(exc))
        return done("schema_failed", 202, {"status": "dead_letter", "reason": "schema_failed"})

    result = process(gateway, body, key)
    email = result.customer.email if result.customer else None

    if result.outcome in _DLQ:
        status, routing_key, source = _DLQ[result.outcome]
        txn = result.event.transaction_id if result.event else None
        await db.finalize_raw_payload(raw_id, status=status,
                                      body_decrypted=result.body_decrypted, error_reason=result.reason)
        await broker.publish(routing_key, {"gateway": gateway,
                             "payload": result.body_decrypted or body, "reason": result.reason}, correlation_id)
        await db.insert_dead_letter(source=source, correlation_id=correlation_id, gateway=gateway,
                                    transaction_id=txn, payload=json.dumps(result.body_decrypted or body),
                                    error_reason=result.reason or "")
        return done(result.outcome.value, 202,
                    {"status": "dead_letter", "reason": result.outcome.value}, email)

    if result.outcome is Outcome.DISCARDED:
        await db.finalize_raw_payload(raw_id, status="discarded", body_decrypted=result.body_decrypted)
        return done("discarded", 200, {"status": "discarded", "event": result.event.event}, email)

    # ACCEPTED: claim the natural key (race-safe) before publishing.
    is_new = await db.reserve_idempotency(result.event.transaction_id, result.event.event, correlation_id)
    if not is_new:
        await db.finalize_raw_payload(raw_id, status="duplicate", body_decrypted=result.body_decrypted)
        return done("duplicate", 200, {"status": "duplicate", "event": result.event.event}, email)

    await broker.publish("lead.received", _lead_message(result, gateway, correlation_id), correlation_id)
    await db.finalize_raw_payload(raw_id, status="routed", body_decrypted=result.body_decrypted)
    return done("accepted", 200, {"status": "accepted", "event": result.event.event}, email)


def create_app(db: Database, broker: Broker, key: bytes) -> FastAPI:
    """Build the app with injected dependencies (used by tests)."""
    app = FastAPI(title="GEX Receiver")

    @app.post("/webhooks/{gateway}")
    async def receive(gateway: str, request: Request):
        return await handle_webhook(db, broker, key, gateway, request)

    return app
