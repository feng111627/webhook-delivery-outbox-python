"""停用/恢复与配置校验。"""
from __future__ import annotations

from app.models import DeliveryStatus, SubscriberStatus

from .conftest import register, submit, wait_terminal


def test_disabled_subscriber_gets_no_delivery(running_client, receiver_server, receiver_port):
    """停用后新事件不再产生投递"""
    client, _app = running_client
    _server, state = receiver_server
    sub_id = register(client, "toggle", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)

    # 停用
    resp = client.post(f"/subscribers/{sub_id}/disable")
    assert resp.status_code == 200
    assert resp.json()["status"] == SubscriberStatus.DISABLED.value

    submit(client, "evt-off", "order.created", {})
    # 没有任何投递产生（事件仍被记录）
    event = client.get("/events/evt-off").json()
    assert event["deliveries"] == []
    assert state.events_for("evt-off") == []


def test_reenabled_subscriber_gets_new_events(running_client, receiver_server, receiver_port):
    """重新启用后新事件恢复送达，停用期间旧事件不补发"""
    client, app = running_client
    _server, state = receiver_server
    worker = app.state.worker
    sub_id = register(client, "toggle2", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)

    client.post(f"/subscribers/{sub_id}/disable")
    submit(client, "evt-gap", "order.created", {})
    assert state.events_for("evt-gap") == []

    # 恢复
    resp = client.post(f"/subscribers/{sub_id}/enable")
    assert resp.status_code == 200
    assert resp.json()["status"] == SubscriberStatus.ACTIVE.value

    submit(client, "evt-after", "order.created", {})
    deliveries = wait_terminal(client, "evt-after", expected=1)
    assert deliveries[0]["status"] == DeliveryStatus.SUCCEEDED.value
    assert len(state.events_for("evt-after")) == 1
    # 停用期间的旧事件不会补发
    assert state.events_for("evt-gap") == []


def test_invalid_url_rejected(client, receiver_port):
    """非法 URL(空/非 http/带凭据等)注册被 422 拒绝且不落库"""
    base = {"name": "x", "secret": "secret-ok-123", "event_types": ["order.created"]}
    for bad_url in [
        "",
        "not-a-url",
        "ftp://127.0.0.1/hook",
        "http://",
        "http://user:pwd@127.0.0.1/hook",
        "file:///etc/passwd",
    ]:
        resp = client.post("/subscribers", json={**base, "url": bad_url})
        assert resp.status_code == 422, (bad_url, resp.text)
        assert "url" in resp.json()["detail"]["error"]
    # 库里没有带病数据
    assert client.get("/subscribers").json()["subscribers"] == []


def test_invalid_secret_rejected(client):
    """非法密钥(空/过短/含空白/超长)被拒绝"""
    base = {"name": "x", "url": "http://127.0.0.1:9/hook", "event_types": ["order.created"]}
    for bad_secret in ["", "short", "with space-in-it", "x" * 300]:
        resp = client.post("/subscribers", json={**base, "secret": bad_secret})
        assert resp.status_code == 422, (bad_secret, resp.text)
        assert "secret" in resp.json()["detail"]["error"]


def test_invalid_event_types_rejected(client):
    """非法事件类型列表(空/重复/非字符串)被拒绝"""
    for bad_types in [[], [""], ["a", "a"], [123]]:
        resp = client.post(
            "/subscribers",
            json={"name": "x", "url": "http://127.0.0.1:9/h", "secret": "secret-ok-123", "event_types": bad_types},
        )
        assert resp.status_code == 422, (bad_types, resp.text)


def test_disable_unknown_subscriber_404(client):
    """停用不存在的订阅方返回 404"""
    resp = client.post("/subscribers/sub_nonexistent/disable")
    assert resp.status_code == 404


def test_event_type_filtering(running_client, receiver_server, receiver_port):
    """订阅方只收到自己订阅的事件类型"""
    client, app = running_client
    _server, state = receiver_server
    register(client, "orders-only", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)
    submit(client, "evt-other", "invoice.issued", {})
    event = client.get("/events/evt-other").json()
    assert event["deliveries"] == []
