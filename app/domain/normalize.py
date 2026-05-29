"""Cleaning/normalization of critical fields (pure, no I/O).

Returns (value, is_valid) tuples so the caller decides routing: an invalid email quarantines the
lead, while an invalid phone only sets a flag and lets the lead proceed.
"""
import re
from dataclasses import dataclass

import phonenumbers

from app.domain.schema import Customer

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_KEEP = re.compile(r"[^\d+]")


@dataclass(frozen=True)
class NormalizedCustomer:
    email: str | None
    email_valid: bool
    first_name: str
    last_name: str | None
    phone_e164: str | None
    phone_valid: bool
    country: str | None


def normalize_email(raw: str | None) -> tuple[str | None, bool]:
    """Trim + lowercase, then validate it has an '@' and a dotted domain."""
    email = (raw or "").strip().lower()
    if not email:
        return None, False
    return email, bool(_EMAIL_RE.match(email))


def normalize_phone(raw: str | None, country: str | None) -> tuple[str | None, bool]:
    """Strip junk (keep digits and '+') and format to E.164 when the number is valid."""
    cleaned = _PHONE_KEEP.sub("", raw or "")
    if not cleaned:
        return None, False
    try:
        number = phonenumbers.parse(cleaned, country or None)
        if phonenumbers.is_valid_number(number):
            return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164), True
    except phonenumbers.NumberParseException:
        pass
    return cleaned, False


def normalize_first_name(raw: str | None) -> str:
    """Default to 'Customer' so downstream integrations never break on an empty name."""
    return (raw or "").strip() or "Customer"


def normalize_customer(customer: Customer) -> NormalizedCustomer:
    email, email_valid = normalize_email(customer.email)
    phone_e164, phone_valid = normalize_phone(customer.phone, customer.country)
    return NormalizedCustomer(
        email=email,
        email_valid=email_valid,
        first_name=normalize_first_name(customer.first_name),
        last_name=(customer.last_name.strip() if customer.last_name else None),
        phone_e164=phone_e164,
        phone_valid=phone_valid,
        country=(customer.country.strip().upper() if customer.country else None),
    )
