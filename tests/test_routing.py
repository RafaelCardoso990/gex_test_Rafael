"""TDD: post-validation routing decision (pure function)."""
import copy

import pytest

from app.domain.routing import Destination, decide
from app.domain.schema import parse_event
from tests.test_schema import VALID


def _event(*, event="order.approved", status="approved"):
    data = copy.deepcopy(VALID)
    data["event"] = event
    data["payment"]["status"] = status
    return parse_event(data)


def test_approved_with_valid_email_goes_to_lead_received():
    assert decide(_event(), email_valid=True) is Destination.LEAD_RECEIVED


def test_approved_with_invalid_email_is_quarantined():
    assert decide(_event(), email_valid=False) is Destination.QUARANTINE


@pytest.mark.parametrize(
    "event, status",
    [
        ("order.refunded", "refunded"),
        ("order.declined", "declined"),
        ("order.pending", "pending"),
        ("order.approved", "declined"),  # event approved but payment not approved
    ],
)
def test_non_approved_is_discarded_regardless_of_email(event, status):
    assert decide(_event(event=event, status=status), email_valid=True) is Destination.DISCARD
    assert decide(_event(event=event, status=status), email_valid=False) is Destination.DISCARD


def test_destination_quarantine_maps_to_schema_failed_queue():
    assert Destination.QUARANTINE.value == "lead.dead.schema_failed"
