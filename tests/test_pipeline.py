"""TDD: the pure receiver pipeline (decrypt -> schema -> normalize -> routing)."""
import base64
import copy
import json
from collections import Counter
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.domain.crypto import load_key
from app.domain.pipeline import Outcome, process
from tests.test_schema import VALID

KEY = bytes(range(32))
DOCS = Path(__file__).resolve().parents[1] / "docs"


def _grummer_body(event: dict, key: bytes = KEY) -> dict:
    plaintext = json.dumps(event).encode()
    iv = b"sixteen-byte-iv!"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return {"iv": base64.b64encode(iv).decode(), "ciphertext": base64.b64encode(ct).decode()}


def test_lous_approved_valid_is_accepted():
    r = process("lous", VALID, KEY)
    assert r.outcome is Outcome.ACCEPTED
    assert r.event.transaction_id == VALID["transaction_id"]
    assert r.customer.email_valid is True
    assert r.body_decrypted is None  # lous arrives in clear text


def test_lous_non_approved_is_discarded():
    data = copy.deepcopy(VALID)
    data["event"], data["payment"]["status"] = "order.refunded", "refunded"
    assert process("lous", data, KEY).outcome is Outcome.DISCARDED


def test_lous_schema_invalid_is_schema_failed():
    data = copy.deepcopy(VALID)
    del data["transaction_id"]
    r = process("lous", data, KEY)
    assert r.outcome is Outcome.SCHEMA_FAILED
    assert "transaction_id" in r.reason


def test_lous_invalid_email_is_quarantined():
    data = copy.deepcopy(VALID)
    data["customer"]["email"] = "broken@nodomain"
    assert process("lous", data, KEY).outcome is Outcome.QUARANTINED


def test_grummer_accepted_exposes_decrypted_body():
    r = process("grummer", _grummer_body(VALID), KEY)
    assert r.outcome is Outcome.ACCEPTED
    assert r.body_decrypted["transaction_id"] == VALID["transaction_id"]


def test_grummer_corrupted_is_decrypt_failed():
    body = _grummer_body(VALID)
    raw = bytearray(base64.b64decode(body["ciphertext"]))
    raw[-1] ^= 0xFF
    body["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    assert process("grummer", body, KEY).outcome is Outcome.DECRYPT_FAILED


def test_grummer_missing_envelope_is_decrypt_failed():
    assert process("grummer", {"foo": "bar"}, KEY).outcome is Outcome.DECRYPT_FAILED


@pytest.mark.skipif(not (DOCS / "webhook_payloads.json").exists(), reason="dataset not present")
def test_full_dataset_reconciliation_matches_gabarito():
    key = load_key((DOCS / "grummer_secret.txt").read_text())
    payloads = json.loads((DOCS / "webhook_payloads.json").read_text())
    counts = Counter(process(e["gateway"], e["body"], key).outcome for e in payloads)

    assert counts[Outcome.DECRYPT_FAILED] == 15
    assert counts[Outcome.DISCARDED] == 20
    # schema_failed bucket = structural failures + quarantined (invalid email)
    assert counts[Outcome.SCHEMA_FAILED] + counts[Outcome.QUARANTINED] == 20
    # accepted still contains the 20 natural-key duplicates; dedup is the receiver's job (DB)
    assert counts[Outcome.ACCEPTED] == 145
