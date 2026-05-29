"""Replay the sample webhooks into the running receiver, then print a status summary.

Usage: RECEIVER_URL=http://localhost:8000 uv run python replay_webhooks.py
The 200 webhooks are sent concurrently — this also exercises the idempotency race end-to-end.
"""
import asyncio
import json
import os
from collections import Counter
from pathlib import Path

import httpx

RECEIVER_URL = os.getenv("RECEIVER_URL", "http://localhost:8000")
PAYLOADS = Path(os.getenv("PAYLOADS", "docs/webhook_payloads.json"))


async def _send(client: httpx.AsyncClient, entry: dict) -> str:
    resp = await client.post(
        f"{RECEIVER_URL}/webhooks/{entry['gateway']}",
        json=entry["body"], headers=entry.get("headers", {}),
    )
    try:
        return resp.json().get("status", f"http_{resp.status_code}")
    except Exception:
        return f"http_{resp.status_code}"


async def main() -> None:
    payloads = json.loads(PAYLOADS.read_text())
    limits = httpx.Limits(max_connections=50)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        results = await asyncio.gather(*(_send(client, e) for e in payloads))
    print(f"Replayed {len(payloads)} webhooks against {RECEIVER_URL}:")
    for status, count in sorted(Counter(results).items()):
        print(f"  {status:>12}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
