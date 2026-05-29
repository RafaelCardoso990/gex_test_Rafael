"""Pure receiver pipeline: decrypt -> schema -> normalize -> routing.

Returns a ProcessResult describing the outcome and the data the receiver needs to persist/publish.
No I/O here (the AES key is injected), so the whole decision flow is unit-testable without infra.
Idempotency (the natural-key dedup) is intentionally left to the receiver, since it needs the DB.
"""
import json
from dataclasses import dataclass
from enum import Enum

from app.domain.crypto import DecryptError, decrypt_grummer
from app.domain.normalize import NormalizedCustomer, normalize_customer
from app.domain.routing import Destination, decide
from app.domain.schema import SaleEvent, SchemaError, parse_event


class Outcome(str, Enum):
    DECRYPT_FAILED = "decrypt_failed"
    SCHEMA_FAILED = "schema_failed"
    QUARANTINED = "quarantined"   # invalid email -> lead.dead.schema_failed
    DISCARDED = "discarded"       # not approved -> raw_payloads only
    ACCEPTED = "accepted"         # ready for the lead.received queue


@dataclass(frozen=True)
class ProcessResult:
    outcome: Outcome
    body_decrypted: dict | None = None  # grummer plaintext, for raw_payloads.body_decrypted
    event: SaleEvent | None = None
    customer: NormalizedCustomer | None = None
    reason: str | None = None           # failure/quarantine detail for the DLQ


def process(gateway: str, body: dict, key: bytes) -> ProcessResult:
    # 1. Obtain the event dict (decrypt grummer; lous is already clear text).
    if gateway == "grummer":
        try:
            event_dict = json.loads(decrypt_grummer(body["iv"], body["ciphertext"], key))
        except (DecryptError, KeyError, TypeError, ValueError) as exc:
            return ProcessResult(Outcome.DECRYPT_FAILED, reason=str(exc))
        decrypted = event_dict
    else:
        event_dict, decrypted = body, None

    # 2. Structural schema validation.
    try:
        event = parse_event(event_dict)
    except SchemaError as exc:
        return ProcessResult(Outcome.SCHEMA_FAILED, body_decrypted=decrypted, reason=str(exc))

    # 3. Normalization + 4. routing decision.
    customer = normalize_customer(event.customer)
    destination = decide(event, customer.email_valid)

    if destination is Destination.DISCARD:
        return ProcessResult(Outcome.DISCARDED, body_decrypted=decrypted, event=event, customer=customer)
    if destination is Destination.QUARANTINE:
        return ProcessResult(
            Outcome.QUARANTINED, body_decrypted=decrypted, event=event, customer=customer,
            reason="invalid customer email",
        )
    return ProcessResult(Outcome.ACCEPTED, body_decrypted=decrypted, event=event, customer=customer)
