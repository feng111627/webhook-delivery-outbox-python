"""重试退避与终态分类：
- 5xx 持续失败 -> 重试到上限 -> retries_exhausted
- 端口不可达 -> 连接失败 -> unreachable
- 401 signature_invalid -> 立即终态 signature_invalid
- 401 timestamp_skew -> 立即终态 timestamp_skew
- 404 -> 立即终态 permanent_reject
每次尝试（attempt）都可查询。
"""
from __future__ import annotations

import time

from app.models import DeliveryStatus, FailureReason

from .conftest import register, submit, wait_terminal


def _delivery_detail(client, delivery_id: str) -> dict:
    resp = client.get(f"/deliveries/{delivery_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_retries_until_exhausted(running_client, receiver_port):
    """对 500 按指数退避重试，达到上限后终态 retries_exhausted 且可查每次尝试"""
    client, app = running_client
    settings = app.state.settings
    register(client, "always-500", "/hook/500", "secret-ok-123", ["order.created"], receiver_port)
    submit(client, "evt-500", "order.created", {})

    deliveries = wait_terminal(client, "evt-500", expected=1, timeout=10)
    d = deliveries[0]
    assert d["status"] == DeliveryStatus.FAILED.value
    assert d["failure_reason"] == FailureReason.RETRIES_EXHAUSTED.value
    assert d["attempts"] == settings.max_attempts

    detail = _delivery_detail(client, d["id"])
    attempts = detail["attempt_history"]
    assert len(attempts) == settings.max_attempts
    assert all(a["outcome"] == "failure" for a in attempts)
    assert all(a["http_status"] == 500 for a in attempts)
    # 尝试按发生顺序记录，时间单调
    nos = [a["attempt_no"] for a in attempts]
    assert nos == list(range(1, settings.max_attempts + 1))
    assert all(attempts[i]["finished_at"] <= attempts[i + 1]["finished_at"] for i in range(len(attempts) - 1))
    # 退避间隔：第 n 次失败后至少等待 base * 2^(n-1)
    for i in range(len(attempts) - 1):
        gap = attempts[i + 1]["started_at"] - attempts[i]["finished_at"]
        expected_gap = settings.base_backoff_seconds * (2**i)
        assert gap + 0.05 >= expected_gap, (i, gap, expected_gap)


def test_unreachable_classified(running_client):
    """订阅方端口不可达：终态原因归类为 unreachable"""
    client, _app = running_client
    # 127.0.0.1:1 上没有监听 -> 连接被拒
    resp = client.post(
        "/subscribers",
        json={
            "name": "dead",
            "url": "http://127.0.0.1:1/hook/ok",
            "secret": "secret-ok-123",
            "event_types": ["order.created"],
        },
    )
    assert resp.status_code == 201
    submit(client, "evt-dead", "order.created", {})

    deliveries = wait_terminal(client, "evt-dead", expected=1, timeout=10)
    d = deliveries[0]
    assert d["status"] == DeliveryStatus.FAILED.value
    assert d["failure_reason"] == FailureReason.UNREACHABLE.value
    detail = _delivery_detail(client, d["id"])
    assert all(a["error_kind"] == "connection" for a in detail["attempt_history"])


def test_signature_failure_classified(running_client, receiver_port):
    """接收方判签名无效(401)：不重试，终态 signature_invalid"""
    client, _app = running_client
    # /hook/badsig 固定回 401 signature_invalid，模拟密钥不匹配
    register(client, "bad-sig", "/hook/badsig", "secret-ok-123", ["order.created"], receiver_port)
    submit(client, "evt-badsig", "order.created", {})
    deliveries = wait_terminal(client, "evt-badsig", expected=1, timeout=5)
    d = deliveries[0]
    assert d["status"] == DeliveryStatus.FAILED.value
    assert d["failure_reason"] == FailureReason.SIGNATURE_INVALID.value
    # 签名类错误不重试
    assert d["attempts"] == 1
    detail = _delivery_detail(client, d["id"])
    assert detail["attempt_history"][0]["receiver_code"] == "signature_invalid"


def test_timestamp_skew_classified(running_client, receiver_port):
    """接收方判时间戳超窗(401)：不重试，终态 timestamp_skew"""
    client, _app = running_client
    register(client, "old-skew", "/hook/oldskew", "secret-ok-123", ["order.created"], receiver_port)
    submit(client, "evt-skew", "order.created", {})
    deliveries = wait_terminal(client, "evt-skew", expected=1, timeout=5)
    d = deliveries[0]
    assert d["status"] == DeliveryStatus.FAILED.value
    assert d["failure_reason"] == FailureReason.TIMESTAMP_SKEW.value
    assert d["attempts"] == 1


def test_permanent_4xx_no_retry(running_client, receiver_port):
    """其它 4xx(404)：不重试，终态 permanent_reject"""
    client, _app = running_client
    register(client, "gone", "/hook/404", "secret-ok-123", ["order.created"], receiver_port)
    submit(client, "evt-404", "order.created", {})
    deliveries = wait_terminal(client, "evt-404", expected=1, timeout=5)
    d = deliveries[0]
    assert d["status"] == DeliveryStatus.FAILED.value
    assert d["failure_reason"] == FailureReason.PERMANENT_REJECT.value
    assert d["attempts"] == 1


def test_failure_reasons_are_distinct(running_client, receiver_port):
    """同一系统里不同失败原因必须能被明确区分开。"""
    client, _ = running_client
    register(client, "a-500", "/hook/500", "secret-ok-123", ["e.t"], receiver_port)
    register(client, "a-sig", "/hook/badsig", "secret-ok-123", ["e.t"], receiver_port)
    register(client, "a-skew", "/hook/oldskew", "secret-ok-123", ["e.t"], receiver_port)
    client.post(
        "/subscribers",
        json={"name": "a-dead", "url": "http://127.0.0.1:1/x", "secret": "secret-ok-123", "event_types": ["e.t"]},
    )
    submit(client, "evt-multi", "e.t", {})
    deliveries = wait_terminal(client, "evt-multi", expected=4, timeout=10)
    reasons = {d["failure_reason"] for d in deliveries}
    assert reasons == {
        FailureReason.RETRIES_EXHAUSTED.value,
        FailureReason.SIGNATURE_INVALID.value,
        FailureReason.TIMESTAMP_SKEW.value,
        FailureReason.UNREACHABLE.value,
    }


def test_timeout_retried_then_exhausted(running_client, receiver_port):
    """接收端处理时间超过请求超时：每次尝试 error_kind=timeout，最终 retries_exhausted。"""
    client, app = running_client
    # 全局超时 5s，这里改用更短超时的独立应用，避免用例变慢
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.config import Settings

    short_settings = Settings(
        max_attempts=2,
        base_backoff_seconds=0.02,
        max_backoff_seconds=0.1,
        request_timeout_seconds=0.3,
        poll_interval_seconds=0.01,
        lock_seconds=5.0,
    )
    app2 = create_app(settings=short_settings, start_worker=True, db_path=":memory:")
    with TestClient(app2) as c2:
        register(c2, "slow-timeout", "/hook/slow?delay=2", "secret-slow-123", ["order.created"], receiver_port)
        submit(c2, "evt-timeout", "order.created", {})
        deliveries = wait_terminal(c2, "evt-timeout", expected=1, timeout=8)
        d = deliveries[0]
        assert d["status"] == DeliveryStatus.FAILED.value
        assert d["failure_reason"] == FailureReason.RETRIES_EXHAUSTED.value
        assert d["attempts"] == 2
        detail = c2.get(f"/deliveries/{d['id']}").json()
        assert all(a["error_kind"] == "timeout" for a in detail["attempt_history"])
