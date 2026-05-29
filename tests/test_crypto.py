"""TDD: AES-256-CBC/PKCS7 decryption for the grummer gateway."""
import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.domain.crypto import DecryptError, decrypt_grummer, load_key

KEY = bytes(range(32))  # arbitrary 32-byte key for self-contained round-trip tests
DOCS = Path(__file__).resolve().parents[1] / "docs"


def _encrypt(plaintext: bytes, key: bytes) -> tuple[str, str]:
    """Test helper: produce (iv_b64, ciphertext_b64) the way the grummer gateway would."""
    iv = b"sixteen-byte-iv!"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv).decode(), base64.b64encode(ciphertext).decode()


def test_decrypt_roundtrip_returns_plaintext():
    plaintext = b'{"transaction_id": "ORD-1", "event": "order.approved"}'
    iv_b64, ct_b64 = _encrypt(plaintext, KEY)
    assert json.loads(decrypt_grummer(iv_b64, ct_b64, KEY))["transaction_id"] == "ORD-1"


def test_decrypt_corrupted_ciphertext_raises_decrypt_error():
    iv_b64, ct_b64 = _encrypt(b'{"x": 1}', KEY)
    tampered = bytearray(base64.b64decode(ct_b64))
    tampered[-1] ^= 0xFF  # break the last block -> PKCS7 unpadding fails
    bad_ct = base64.b64encode(bytes(tampered)).decode()
    with pytest.raises(DecryptError):
        decrypt_grummer(iv_b64, bad_ct, KEY)


def test_decrypt_wrong_key_raises_decrypt_error():
    iv_b64, ct_b64 = _encrypt(b'{"x": 1}', KEY)
    with pytest.raises(DecryptError):
        decrypt_grummer(iv_b64, ct_b64, bytes(range(1, 33)))


def test_load_key_accepts_32_bytes_and_rejects_others():
    assert len(load_key("00" * 32)) == 32
    with pytest.raises(ValueError):
        load_key("00" * 16)


@pytest.mark.skipif(not (DOCS / "webhook_payloads.json").exists(), reason="dataset not present")
def test_real_dataset_matches_gabarito():
    """Golden fixture: 96 grummer payloads decrypt, 15 fail (= dlq_decrypt_failed)."""
    key = load_key((DOCS / "grummer_secret.txt").read_text())
    payloads = json.loads((DOCS / "webhook_payloads.json").read_text())
    ok = fail = 0
    for entry in payloads:
        if entry["gateway"] != "grummer":
            continue
        try:
            json.loads(decrypt_grummer(entry["body"]["iv"], entry["body"]["ciphertext"], key))
            ok += 1
        except DecryptError:
            fail += 1
    assert (ok, fail) == (96, 15)
