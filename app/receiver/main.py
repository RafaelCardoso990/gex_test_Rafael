"""Production ASGI entrypoint: `uvicorn app.receiver.main:app`. Connects deps on startup."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

from app.adapters.broker import Broker
from app.adapters.db import Database
from app.config import load_settings
from app.domain.crypto import load_key
from app.logging import configure_logging
from app.receiver.app import handle_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = load_settings()
    app.state.db = await Database.connect(settings)
    app.state.broker = await Broker.connect(settings.rabbitmq_url)
    await app.state.broker.declare_topology()
    app.state.key = load_key(Path(settings.grummer_key_path).read_text())
    yield
    await app.state.broker.close()
    await app.state.db.close()


app = FastAPI(title="GEX Receiver", lifespan=lifespan)


@app.post("/webhooks/{gateway}")
async def receive(gateway: str, request: Request):
    state = request.app.state
    return await handle_webhook(state.db, state.broker, state.key, gateway, request)


@app.get("/health")
async def health():
    return {"status": "ok"}
