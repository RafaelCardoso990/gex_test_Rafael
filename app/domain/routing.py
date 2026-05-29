"""Routing decision for a payload that already passed decrypt + schema validation (pure).

Decrypt and schema failures are routed by the receiver directly from the raised exceptions; this
function only decides what to do with a structurally valid event.
"""
from enum import Enum

from app.domain.schema import SaleEvent


class Destination(str, Enum):
    LEAD_RECEIVED = "lead.received"          # approved + valid email -> processed
    QUARANTINE = "lead.dead.schema_failed"   # approved but invalid email -> separated
    DISCARD = "discard"                      # not approved -> recorded only in raw_payloads


def decide(event: SaleEvent, email_valid: bool) -> Destination:
    is_approved = event.event == "order.approved" and event.payment.status == "approved"
    if not is_approved:
        return Destination.DISCARD
    if not email_valid:
        return Destination.QUARANTINE
    return Destination.LEAD_RECEIVED
