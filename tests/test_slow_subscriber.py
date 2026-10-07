"""慢订阅方不能拖住其它订阅方或后续事件。"""
from __future__ import annotations

import time

from app.models import DeliveryStatus

from .conftest import register, submit, wait_terminal


def test_slow_subscriber_does_not_block_others(running_client, receiver_server, receiver_port):
    """1.5s 慢订阅方不阻塞同事件的快订阅方与后续事件"""
    client, app = running_client
    _server, state = receiver_server
    worker = app.state.worker

    # 慢订阅方：处理一个请求要 1.5 秒
    slow_id = register(
        client, "slow", "/hook/slow?delay=1.5", "secret-slow-123", ["order.created"], receiver_port
    )
    # 健康订阅方：瞬时 200
    fast_id = register(
        client, "fast", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port
    )

    start = time.monotonic()
    submit(client, "evt-slow-1", "order.created", {"n": 1})

    # 快订阅方应在远小于慢订阅方耗时的时间内成功
    deadline = time.monotonic() + 1.0
    fast_succeeded = False
    while time.monotonic() < deadline:
        resp = client.get(
            "/deliveries",
            params={"event_id": "evt-slow-1", "subscriber_id": fast_id},
        )
        items = resp.json()["deliveries"]
        if items and items[0]["status"] == DeliveryStatus.SUCCEEDED.value:
            fast_succeeded = True
            break
        time.sleep(0.02)
    fast_elapsed = time.monotonic() - start
    assert fast_succeeded, "快订阅方没有在 1s 内完成投递"
    assert fast_elapsed < 1.0, f"快订阅方被慢订阅方拖住了: {fast_elapsed:.2f}s"

    # 在慢订阅方仍在处理期间，后续事件给快订阅方也应当照常送达
    submit(client, "evt-slow-2", "order.created", {"n": 2})
    deliveries2 = wait_terminal(client, "evt-slow-2", expected=2, timeout=3.0)
    fast2 = next(d for d in deliveries2 if d["subscriber_id"] == fast_id)
    assert fast2["status"] == DeliveryStatus.SUCCEEDED.value

    # 慢订阅方最终也成功（约 1.5 秒），整个过程没有阻塞别人
    deliveries1 = wait_terminal(client, "evt-slow-1", expected=2, timeout=5.0)
    slow = next(d for d in deliveries1 if d["subscriber_id"] == slow_id)
    assert slow["status"] == DeliveryStatus.SUCCEEDED.value
    assert len(state.events_for("evt-slow-1")) == 2
    assert len(state.events_for("evt-slow-2")) == 2
