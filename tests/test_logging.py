"""TDD: PII-safe log anonymization."""
from app.logging import anonymize


def test_anonymize_is_stable_and_hides_email():
    out = anonymize("Foo@Bar.com")
    assert out == anonymize("Foo@Bar.com")  # deterministic
    assert "Foo@Bar.com" not in out          # original never leaks
    assert out.startswith("cust_")


def test_anonymize_handles_missing_email():
    assert anonymize(None) == "anonymous"
    assert anonymize("") == "anonymous"
