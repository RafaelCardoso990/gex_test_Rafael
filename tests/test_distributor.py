"""SMS distributor: send simulation (pure) and delivery/DLQ with status update (integration)."""
import pytest

from app.workers.consumer import handle_lead
from app.workers.distributor import SmsSendError, deliver, send_sms
from tests.test_consumer import LEAD_MSG


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHTTP:
    """Records POST calls and returns a configurable status code."""
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _FakeResponse(self.status_code)


class _RNG:
    """Deterministic RNG stub: random() always returns `value`."""
    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


# --- send simulation (no infra) ---

async def test_send_sms_raises_on_simulated_failure():
    http = _FakeHTTP()
    with pytest.raises(SmsSendError):
        await send_sms(http, "http://x", {}, rng=_RNG(0.0), fail_rate=0.10)  # 0.0 < 0.10 -> fail
    assert http.calls == []  # never reached the POST


async def test_send_sms_posts_when_not_failing():
    http = _FakeHTTP(status_code=200)
    await send_sms(http, "http://x", {"a": 1}, rng=_RNG(0.99), fail_rate=0.10)  # 0.99 -> no fail
    assert http.calls == [("http://x", {"a": 1})]


# --- delivery + status update (real MySQL + RabbitMQ) ---

async def _sms_message(db, broker, drain) -> dict:
    """Run the consumer to create the pending channels, then grab the dist.sms message."""
    await handle_lead(db, broker, LEAD_MSG)
    return (await drain("dist.sms"))[0]


async def test_deliver_success_marks_delivered_with_lag(db, broker, drain, query):
    message = await _sms_message(db, broker, drain)
    outcome = await deliver(db, broker, _FakeHTTP(200), "http://x", message, rng=_RNG(0.99))
    assert outcome == "delivered"
    rows = await query(
        "SELECT status, delivered_at, lag_db_to_channel_seconds "
        "FROM distribution_status WHERE channel = 'SMS'"
    )
    status, delivered_at, lag = rows[0]
    assert status == "delivered"
    assert delivered_at is not None
    assert lag is not None  # DB->channel lag computed


async def test_deliver_failure_goes_to_dlq_and_marks_dead(db, broker, drain, query):
    message = await _sms_message(db, broker, drain)

    async def no_sleep(_seconds):
        pass

    outcome = await deliver(db, broker, _FakeHTTP(), "http://x", message,
                            rng=_RNG(0.0), backoff=(0, 0, 0), sleep=no_sleep)
    assert outcome == "dead"
    assert (await query("SELECT status FROM distribution_status WHERE channel='SMS'"))[0][0] == "dead"
    assert len(await drain("dist.dead.sms")) == 1
    assert (await query("SELECT source FROM lead_dead_letter"))[0][0] == "dist.sms"
