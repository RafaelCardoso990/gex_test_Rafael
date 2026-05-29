"""Shared fixtures: an ephemeral MySQL (testcontainers) with the schema applied."""
import json
import time
from pathlib import Path

import aio_pika
import pymysql
import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.mysql import MySqlContainer
from testcontainers.rabbitmq import RabbitMqContainer

from app.adapters.broker import Broker
from app.adapters.db import Database
from app.config import Settings
from app.receiver.app import create_app

TEST_KEY = bytes(range(32))  # 32-byte AES key used by grummer test payloads
_TABLES = ("distribution_status", "lead_events", "orders", "leads",
           "idempotency_keys", "raw_payloads", "lead_dead_letter")

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _apply_sql_file(cursor, path: Path) -> None:
    # Strip -- comments first (some contain ';'), then split into statements. Safe for 001/002,
    # which have no string literals containing '--' or ';'.
    lines = (line.split("--", 1)[0] for line in path.read_text().splitlines())
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            cursor.execute(statement)


def _apply_stored_proc(cursor, path: Path) -> None:
    # The proc uses DELIMITER (a client directive pymysql does not understand). We send the whole
    # CREATE PROCEDURE ... END body as a single statement (the text between the two $$ markers).
    body = path.read_text().split("$$")[1].strip()
    cursor.execute("DROP PROCEDURE IF EXISTS sp_insert_lead")
    cursor.execute(body)


@pytest.fixture(scope="session")
def mysql_container():
    with MySqlContainer(
        "mysql:8", root_password="root", username="gex", password="gex", dbname="gex"
    ) as container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(3306))
        conn = None
        for _ in range(30):  # the container logs "ready" but accept a brief connect retry
            try:
                conn = pymysql.connect(host=host, port=port, user="root", password="root", database="gex")
                break
            except pymysql.err.OperationalError:
                time.sleep(1)
        assert conn is not None, "could not connect to the MySQL test container"
        try:
            with conn.cursor() as cur:
                _apply_sql_file(cur, SQL_DIR / "001_create_tables.sql")
                _apply_sql_file(cur, SQL_DIR / "002_indexes.sql")
                _apply_stored_proc(cur, SQL_DIR / "003_stored_procs.sql")
            conn.commit()
        finally:
            conn.close()
        yield container


def _settings_for(container) -> Settings:
    return Settings(
        mysql_host=container.get_container_host_ip(),
        mysql_port=int(container.get_exposed_port(3306)),
        mysql_user="root",
        mysql_password="root",
        mysql_db="gex",
        rabbitmq_url="",
        grummer_key_path="",
        sms_webhook_url="",
    )


@pytest.fixture
async def db(mysql_container):
    database = await Database.connect(_settings_for(mysql_container))
    async with database._pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in _TABLES:
            await cur.execute(f"TRUNCATE {table}")
        await cur.execute("SET FOREIGN_KEY_CHECKS=1")
    yield database
    await database.close()


@pytest.fixture
async def query(db):
    """Run a read query against the test DB (for assertions)."""
    async def _run(sql: str, args: tuple = ()):
        async with db._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, args)
            return await cur.fetchall()
    return _run


@pytest.fixture(scope="session")
def rabbitmq_container():
    with RabbitMqContainer("rabbitmq:3.13-alpine", username="guest", password="guest") as container:
        yield container


@pytest.fixture
def rabbitmq_url(rabbitmq_container) -> str:
    host = rabbitmq_container.get_container_host_ip()
    port = rabbitmq_container.get_exposed_port(5672)
    return f"amqp://guest:guest@{host}:{port}/"


@pytest.fixture
async def broker(rabbitmq_url):
    broker = await Broker.connect(rabbitmq_url)
    await broker.declare_topology()
    await broker.purge_all()  # clean slate per test
    yield broker
    await broker.close()


@pytest.fixture
async def client(db, broker):
    """httpx client wired to the receiver app over ASGI (no network), backed by the test infra."""
    app = create_app(db, broker, TEST_KEY)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
def drain(rabbitmq_url):
    """Return an async helper that drains and acks all messages from a queue, returning their bodies."""
    async def _drain(queue_name: str) -> list[dict]:
        conn = await aio_pika.connect_robust(rabbitmq_url)
        try:
            channel = await conn.channel()
            queue = await channel.get_queue(queue_name)
            messages = []
            while (message := await queue.get(fail=False)) is not None:
                await message.ack()
                messages.append(json.loads(message.body))
            return messages
        finally:
            await conn.close()
    return _drain
