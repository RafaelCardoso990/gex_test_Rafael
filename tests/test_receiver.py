"""Integration: the FastAPI receiver end-to-end (real MySQL + RabbitMQ via testcontainers)."""
import base64
import copy
import json

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from tests.conftest import TEST_KEY
from tests.test_schema import VALID


def _grummer_body(event: dict) -> dict:
    plaintext = json.dumps(event).encode()
    iv = b"sixteen-byte-iv!"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(TEST_KEY), modes.CBC(iv)).encryptor()
    ct = enc.update(padded) + enc.finalize()
    return {"iv": base64.b64encode(iv).decode(), "ciphertext": base64.b64encode(ct).decode()}


async def test_lous_approved_is_accepted_and_published(client, drain, query):
    resp = await client.post("/webhooks/lous", json=VALID)
    assert resp.status_code == 200 and resp.json()["status"] == "accepted"

    assert (await query("SELECT processing_status FROM raw_payloads"))[0][0] == "routed"
    assert (await query("SELECT transaction_id, event FROM idempotency_keys"))[0] == (
        VALID["transaction_id"], "order.approved",
    )
    messages = await drain("lead.received")
    assert len(messages) == 1
    assert messages[0]["transaction_id"] == VALID["transaction_id"]
    assert messages[0]["customer"]["email"] == VALID["customer"]["email"].lower()


async def test_duplicate_webhook_publishes_once(client, drain):
    first = await client.post("/webhooks/lous", json=VALID)
    second = await client.post("/webhooks/lous", json=VALID)
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert len(await drain("lead.received")) == 1


async def test_non_approved_is_discarded(client, drain):
    data = copy.deepcopy(VALID)
    data["event"], data["payment"]["status"] = "order.refunded", "refunded"
    resp = await client.post("/webhooks/lous", json=data)
    assert resp.json()["status"] == "discarded"
    assert await drain("lead.received") == []


async def test_grummer_corrupted_goes_to_decrypt_dlq(client, drain, query):
    body = _grummer_body(VALID)
    raw = bytearray(base64.b64decode(body["ciphertext"]))
    raw[-1] ^= 0xFF
    body["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    resp = await client.post("/webhooks/grummer", json=body, headers={"X-GR-Encrypted": "true"})
    assert resp.status_code == 202 and resp.json()["reason"] == "decrypt_failed"
    assert len(await drain("lead.dead.decrypt_failed")) == 1
    assert (await query("SELECT source FROM lead_dead_letter"))[0][0] == "decrypt"


async def test_grummer_valid_persists_decrypted_body(client, query):
    resp = await client.post("/webhooks/grummer", json=_grummer_body(VALID),
                             headers={"X-GR-Encrypted": "true"})
    assert resp.json()["status"] == "accepted"
    assert (await query("SELECT body_decrypted FROM raw_payloads"))[0][0] is not None


async def test_unknown_gateway_returns_404(client):
    assert (await client.post("/webhooks/unknown", json={})).status_code == 404
