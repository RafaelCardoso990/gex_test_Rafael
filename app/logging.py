"""Structured JSON logging. PII (email/phone) is never logged in clear text."""
import hashlib
import logging

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger():
    return structlog.get_logger()


def anonymize(email: str | None) -> str:
    """Stable, non-reversible customer id for logs. Never exposes the raw email."""
    if not email:
        return "anonymous"
    return "cust_" + hashlib.sha256(email.encode()).hexdigest()[:16]
