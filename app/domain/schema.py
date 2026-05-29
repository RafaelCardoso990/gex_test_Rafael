"""Sale event schema and structural validation (pure, no I/O).

Validates only the *structure* (required fields, parseable types). Email/phone *format* is handled
downstream by app.domain.normalize. A SchemaError carries a concise reason for the DLQ detail.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SchemaError(Exception):
    """Raised when the payload does not satisfy the required schema."""


class Customer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: str = Field(min_length=1)
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    country: str | None = None


class Product(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    name: str | None = None
    niche: str | None = None
    quantity: int | None = None


class Payment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str = Field(min_length=1)
    amount_usd: float | None = None
    method: str | None = None


class SaleEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    transaction_id: str = Field(min_length=1)
    transaction_time: datetime
    event: str = Field(min_length=1)
    customer: Customer
    product: Product
    payment: Payment


def parse_event(data: dict) -> SaleEvent:
    """Validate a raw event dict into a typed SaleEvent, or raise SchemaError."""
    try:
        return SaleEvent.model_validate(data)
    except ValidationError as exc:
        raise SchemaError(_summarize(exc)) from exc


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )
