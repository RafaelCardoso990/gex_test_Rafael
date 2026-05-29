"""TDD: cleaning/normalization of email, phone (E.164) and name."""
import pytest

from app.domain.normalize import (
    NormalizedCustomer,
    normalize_customer,
    normalize_email,
    normalize_first_name,
    normalize_phone,
)
from app.domain.schema import Customer


@pytest.mark.parametrize(
    "raw, expected, valid",
    [
        ("  Elizabeth.Walker@GMAIL.com ", "elizabeth.walker@gmail.com", True),
        ("john@example.co.uk", "john@example.co.uk", True),
        ("notanemail", "notanemail", False),
        ("missing@domain", "missing@domain", False),  # no dot in domain
        ("", None, False),
        (None, None, False),
    ],
)
def test_normalize_email(raw, expected, valid):
    assert normalize_email(raw) == (expected, valid)


@pytest.mark.parametrize(
    "raw, country, expected, valid",
    [
        ("+16502530000", None, "+16502530000", True),
        ("+1 (650) 253-0000", "US", "+16502530000", True),  # messy but valid -> E.164
        ("+44 7911 123456", "GB", "+447911123456", True),
        ("+1234", None, "+1234", False),  # too short, flagged but kept
        ("abc", None, None, False),
        ("", None, None, False),
        (None, None, None, False),
    ],
)
def test_normalize_phone(raw, country, expected, valid):
    assert normalize_phone(raw, country) == (expected, valid)


@pytest.mark.parametrize(
    "raw, expected",
    [("John", "John"), ("  Mary ", "Mary"), ("", "Customer"), ("   ", "Customer"), (None, "Customer")],
)
def test_normalize_first_name(raw, expected):
    assert normalize_first_name(raw) == expected


def test_normalize_customer_valid():
    c = Customer(
        email="  FOO@Bar.com ", first_name="", last_name="Walker",
        phone="+1 (650) 253-0000", country="us",
    )
    n = normalize_customer(c)
    assert isinstance(n, NormalizedCustomer)
    assert n.email == "foo@bar.com" and n.email_valid is True
    assert n.first_name == "Customer"  # empty -> default
    assert n.phone_e164 == "+16502530000" and n.phone_valid is True
    assert n.country == "US"  # uppercased


def test_normalize_customer_invalid_email_and_phone():
    c = Customer(email="broken@nodomain", first_name="Ann", phone="+1234", country="US")
    n = normalize_customer(c)
    assert n.email_valid is False  # lead is quarantined upstream
    assert n.phone_valid is False  # flagged but lead proceeds
    assert n.first_name == "Ann"
