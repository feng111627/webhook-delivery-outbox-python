import pytest

from webhook.config import Settings
from webhook.db import connect, init_db
from webhook.delivery import DeliveryResult
from webhook.repositories import (
    DeliveryRepository,
    EventRepository,
    SubscriptionRepository,
)
from webhook.service import WebhookService
from webhook.worker import DeliveryWorker


class FakeDeliverer:
    def __init__(self, results):
        # results: list[DeliveryResult] 依次返回，之后重复最后一个
        self.results = results
        self.calls = []

    async def deliver(self, url, secret, event_id, event_type, payload, timestamp=None):
        self.calls.append((url, event_id))
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[idx]


def make_env(tmp_path, **settings_kw):
    conn = connect(str(tmp_path / "w.db"))
    init_db(conn)
    subs, events, dels = (
        SubscriptionRepository(conn),
        EventRepository(conn),
        DeliveryRepository(conn),
    )
    settings = Settings(
        database_path=str(tmp_path / "w.db"),
        poll_interval=1000,
        base_delay=0.001,
        max_delay=0.001,
        **settings_kw,
    )
    return conn, WebhookService(subs, events, dels), subs, events, dels, settings


async def run_to_terminal(worker, max_rounds=50):
    for _ in range(max_rounds):
        n = await worker.run_once()
        if n == 0:
            break
    else:  # pragma: no cover
        raise AssertionError("delivery never reached terminal state")


@pytest.mark.asyncio
async def test_retry_to_exhausted_with_non_2xx(tmp_path):
    conn, svc, subs, events, dels, settings = make_env(tmp_path, max_attempts=4)
    svc.register_subscription("s", "http://x/hook", "0123456789abcdef", ["t"])
    svc.submit_event("e1", "t", {})
    fake = FakeDeliverer(
        [DeliveryResult(False, 503, "non_2xx", None, "busy")]
    )
    worker = DeliveryWorker(subs, events, dels, fake, settings)
    await run_to_terminal(worker)
    status = svc.get_event_status("e1")["deliveries"][0]
    assert status["status"] == "failed_retries_exhausted"
    assert status["attempts"] == 4
    detail = svc.get_delivery_detail(1)
    assert [a["attempt_no"] for a in detail["attempts"]] == [1, 2, 3, 4]
    assert all(a["status_code"] == 503 for a in detail["attempts"])


@pytest.mark.asyncio
async def test_unreachable_terminal(tmp_path):
    conn, svc, subs, events, dels, settings = make_env(tmp_path, max_attempts=3)
    svc.register_subscription("s", "http://127.0.0.1:1/hook", "0123456789abcdef", ["t"])
    svc.submit_event("e1", "t", {})
    fake = FakeDeliverer(
        [DeliveryResult(False, None, "connection_error", "ConnectError: refused")]
    )
    worker = DeliveryWorker(subs, events, dels, fake, settings)
    await run_to_terminal(worker)
    status = svc.get_event_status("e1")["deliveries"][0]
    assert status["status"] == "failed_subscriber_unreachable"
    assert status["last_failure_reason"] == "connection_error"


@pytest.mark.asyncio
async def test_signature_rejected_is_immediate_terminal(tmp_path):
    conn, svc, subs, events, dels, settings = make_env(tmp_path, max_attempts=5)
    svc.register_subscription("s", "http://x/hook", "0123456789abcdef", ["t"])
    svc.submit_event("e1", "t", {})
    fake = FakeDeliverer(
        [DeliveryResult(False, 401, "signature_rejected", None, "bad sig")]
    )
    worker = DeliveryWorker(subs, events, dels, fake, settings)
    await run_to_terminal(worker)
    status = svc.get_event_status("e1")["deliveries"][0]
    assert status["status"] == "failed_signature_rejected"
    assert status["attempts"] == 1


@pytest.mark.asyncio
async def test_success_after_retry(tmp_path):
    conn, svc, subs, events, dels, settings = make_env(tmp_path, max_attempts=3)
    svc.register_subscription("s", "http://x/hook", "0123456789abcdef", ["t"])
    svc.submit_event("e1", "t", {})
    fake = FakeDeliverer(
        [
            DeliveryResult(False, 500, "non_2xx", None, "boom"),
            DeliveryResult(False, None, "timeout", "ReadTimeout"),
            DeliveryResult(True, 200, "none", None, "ok"),
        ]
    )
    worker = DeliveryWorker(subs, events, dels, fake, settings)
    await run_to_terminal(worker)
    status = svc.get_event_status("e1")["deliveries"][0]
    assert status["status"] == "succeeded"
    assert status["attempts"] == 3
