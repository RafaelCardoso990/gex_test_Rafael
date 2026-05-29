"""TDD: structural schema validation of the sale event (Pydantic)."""
import copy
from datetime import datetime

import pytest

from app.domain.schema import SaleEvent, SchemaError, parse_event

VALID = {
    "transaction_id": "ORD-2026-010015",
    "transaction_time": "2026-05-01T22:24:00.275205+00:00",
    "event": "order.approved",
    "customer": {
        "email": "elizabeth.walker1214@gmail.com",
        "first_name": "Elizabeth",
        "last_name": "Walker",
        "phone": "+16248333008",
        "country": "CA",
    },
    "product": {"id": "PROD-002", "name": "Slim Pro", "niche": "weight_loss", "quantity": 1},
    "payment": {"status": "approved", "amount_usd": 88.6, "method": "credit_card"},
}


def _without(*path):
    """Return a copy of VALID with the nested key at `path` removed."""
    data = copy.deepcopy(VALID)
    node = data
    for key in path[:-1]:
        node = node[key]
    node.pop(path[-1], None)
    return data


def test_valid_event_parses():
    ev = parse_event(VALID)
    assert isinstance(ev, SaleEvent)
    assert ev.transaction_id == "ORD-2026-010015"
    assert ev.event == "order.approved"
    assert isinstance(ev.transaction_time, datetime)
    assert ev.product.quantity == 1  # quantity lives inside product
    assert ev.payment.status == "approved"


@pytest.mark.parametrize(
    "path",
    [
        ("transaction_id",),
        ("event",),
        ("transaction_time",),
        ("product",),
        ("customer", "email"),
        ("payment", "status"),
    ],
)
def test_missing_required_field_raises(path):
    with pytest.raises(SchemaError):
        parse_event(_without(*path))


def test_unparseable_transaction_time_raises():
    bad = copy.deepcopy(VALID)
    bad["transaction_time"] = "not-a-date"
    with pytest.raises(SchemaError):
        parse_event(bad)


def test_empty_required_string_raises():
    bad = copy.deepcopy(VALID)
    bad["transaction_id"] = ""
    with pytest.raises(SchemaError):
        parse_event(bad)


def test_missing_first_name_is_allowed():
    ev = parse_event(_without("customer", "first_name"))
    assert ev.customer.first_name is None  # normalize will apply the 'Customer' default


def test_schema_error_carries_reason():
    try:
        parse_event(_without("transaction_id"))
    except SchemaError as exc:
        assert "transaction_id" in str(exc)
    else:
        pytest.fail("expected SchemaError")
