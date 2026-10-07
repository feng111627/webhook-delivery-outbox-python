"""重复提交 / 并发重复事件不重复投递。"""
from __future__ import annotations

import threading

from app.models import DeliveryStatus

from .conftest import register, submit, wait_terminal


def test_duplicate_submit_creates_one_delivery_per_subscriber(client, receiver_port):
    """同一 event_id 重复提交：每个订阅方只产生一条投递"""
    register(client, "sub-a", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)

    first = submit(client, "evt-dup-1", "order.created", {"n": 1})
    assert first["deduplicated"] is False
    assert len(first["deliveries"]) == 1

    second = submit(client, "evt-dup-1", "order.created", {"n": 1})
    assert second["deduplicated"] is True
    assert second["deliveries"] == []

    # 直接查库层面也只有一条
    resp = client.get("/deliveries", params={"event_id": "evt-dup-1"})
    assert len(resp.json()["deliveries"]) == 1


def test_concurrent_duplicate_submits_deliver_once(running_client, receiver_server, receiver_port):
    """20 线程并发提交同一事件：接收端只被送达一次"""
    client, app = running_client
    _server, state = receiver_server
    worker = app.state.worker

    sub_id = register(client, "sub-c", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)

    errors: list[Exception] = []

    def fire() -> None:
        try:
            submit(client, "evt-race-1", "order.created", {"k": "v"})
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发问题
            errors.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    assert worker.wait_idle(timeout=5)

    deliveries = wait_terminal(client, "evt-race-1", expected=1)
    assert len(deliveries) == 1
    d = deliveries[0]
    assert d["subscriber_id"] == sub_id
    assert d["status"] == DeliveryStatus.SUCCEEDED.value
    assert d["attempts"] == 1
    # 关键断言：接收端只被送达一次
    received = state.events_for("evt-race-1")
    assert len(received) == 1, received
