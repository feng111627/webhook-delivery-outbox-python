"""端到端：慢订阅方隔离、重试终态、不可达、签名拒绝、停用恢复。"""
from __future__ import annotations

import httpx
import pytest

from tests.conftest import wait_for

SECRET = "receiver-local-secret-0123456789"


async def _receiver_events(base: str) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{base}/received")
        return r.json()["events"]


def _delivery(service, event_id: str, name_substr: str | None = None) -> dict:
    st = service.get_event_status(event_id)
    deliveries = st["deliveries"]
    if name_substr:
        deliveries = [d for d in deliveries if name_substr in (d["subscription_name"] or "")]
    assert len(deliveries) == 1
    return deliveries[0]


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_others(components, receiver_base):
    service, worker, _ = components
    service.register_subscription(
        "slow-sub", f"{receiver_base}/hook/slow", SECRET, ["evt"]
    )
    service.register_subscription(
        "fast-sub", f"{receiver_base}/hook/ok", SECRET, ["evt"]
    )
    # 慢订阅方请求头不支持自定义 delay，接收端默认睡 3 秒；这里用短超时
    # 让它快速超时失败。fast-sub 必须照常迅速成功。
    service.submit_event("evt-slow-1", "evt", {})

    fast = await wait_for(
        lambda: (
            d
            if (
                d := next(
                    (
                        x
                        for x in service.get_event_status("evt-slow-1")["deliveries"]
                        if x["subscription_name"] == "fast-sub"
                        and x["status"] == "succeeded"
                    ),
                    None,
                )
            )
            else None
        ),
        timeout=5,
    )
    assert fast["attempts"] == 1

    # 慢订阅方此时仍在进行中/重试中，没有阻塞 fast
    slow_d = next(
        x
        for x in service.get_event_status("evt-slow-1")["deliveries"]
        if x["subscription_name"] == "slow-sub"
    )
    assert slow_d["status"] in ("in_flight", "retrying", "failed_subscriber_unreachable")


@pytest.mark.asyncio
async def test_flaky_succeeds_within_retries(components, receiver_base):
    service, worker, _ = components
    service.register_subscription(
        "flaky-sub", f"{receiver_base}/hook/flaky", SECRET, ["evt"]
    )
    service.submit_event("evt-flaky-1", "evt", {})

    d = await wait_for(
        lambda: (
            x
            if (
                x := _delivery(service, "evt-flaky-1")
            )["status"] == "succeeded"
            else None
        ),
        timeout=10,
    )
    assert d["attempts"] == 3  # 前两次 503，第三次成功
    events = await _receiver_events(receiver_base)
    delivered = [
        e
        for e in events
        if e["event_id"] == "evt-flaky-1" and e.get("delivered")
    ]
    assert len(delivered) == 1  # 失败的重试不算送达，成功只发生一次


@pytest.mark.asyncio
async def test_permanent_fail_reaches_retries_exhausted(components, receiver_base):
    service, worker, _ = components
    service.register_subscription(
        "fail-sub", f"{receiver_base}/hook/fail", SECRET, ["evt"]
    )
    service.submit_event("evt-fail-1", "evt", {})

    async def condition():
        d = _delivery(service, "evt-fail-1")
        return d if d["terminal"] else None

    d = await wait_for(condition, timeout=10)
    assert d["status"] == "failed_retries_exhausted"
    assert d["last_failure_reason"] == "non_2xx"
    detail = service.get_delivery_detail(d["delivery_id"])
    assert len(detail["attempts"]) == 4
    assert {a["status_code"] for a in detail["attempts"]} == {503}


@pytest.mark.asyncio
async def test_connection_refused_is_unreachable(components):
    # 不启动接收端：指向一个必然拒绝连接的本地端口
    service, worker, _ = components
    service.register_subscription(
        "dead-sub",
        "http://127.0.0.1:1/hook/ok",
        SECRET,
        ["evt"],
    )
    service.submit_event("evt-dead-1", "evt", {})

    async def condition():
        d = _delivery(service, "evt-dead-1")
        return d if d["terminal"] else None

    d = await wait_for(condition, timeout=10)
    assert d["status"] == "failed_subscriber_unreachable"
    assert d["last_failure_reason"] == "connection_error"
    assert d["last_response_code"] is None


@pytest.mark.asyncio
async def test_bad_secret_is_signature_terminal(components, receiver_base):
    service, worker, _ = components
    # 订阅方密钥与接收端期望的不一致
    service.register_subscription(
        "badsecret-sub",
        f"{receiver_base}/hook/badsecret",
        "receiver-local-secret-0123456789",
        ["evt"],
    )
    service.submit_event("evt-badsig-1", "evt", {})

    async def condition():
        d = _delivery(service, "evt-badsig-1")
        return d if d["terminal"] else None

    d = await wait_for(condition, timeout=10)
    assert d["status"] == "failed_signature_rejected"
    assert d["attempts"] == 1  # 不重试
    assert d["last_response_code"] == 401


@pytest.mark.asyncio
async def test_disable_and_reenable(components, receiver_base):
    service, worker, _ = components
    sub = service.register_subscription(
        "toggle-sub", f"{receiver_base}/hook/ok", SECRET, ["evt"]
    )
    # 停用后提交：不产生任何投递
    service.set_subscription_enabled(sub["id"], False)
    r = service.submit_event("evt-off-1", "evt", {})
    assert r["matched_subscriptions"] == 0 and r["new_deliveries"] == []

    # 重新启用：新事件正常投递成功
    service.set_subscription_enabled(sub["id"], True)
    service.submit_event("evt-on-1", "evt", {})
    d = await wait_for(
        lambda: (
            x if (x := _delivery(service, "evt-on-1"))["status"] == "succeeded" else None
        ),
        timeout=5,
    )
    assert d["attempts"] == 1
    events = await _receiver_events(receiver_base)
    received_ids = {e["event_id"] for e in events}
    assert "evt-off-1" not in received_ids
    assert "evt-on-1" in received_ids


@pytest.mark.asyncio
async def test_disabling_skips_pending_delivery(components, receiver_base):
    service, worker, _ = components
    sub = service.register_subscription(
        "pending-sub", f"{receiver_base}/hook/fail", SECRET, ["evt"]
    )
    service.submit_event("evt-pending-1", "evt", {})
    # 等它至少进入过一次投递（retrying/in_flight），随后停用
    await wait_for(lambda: _delivery(service, "evt-pending-1")["attempts"] >= 1)
    service.set_subscription_enabled(sub["id"], False)

    async def condition():
        d = _delivery(service, "evt-pending-1")
        return d if d["terminal"] else None

    d = await wait_for(condition, timeout=5)
    assert d["status"] == "skipped_subscriber_disabled"
