"""端到端 HTTP API 测试。"""
from __future__ import annotations

from app.models import DeliveryStatus, FailureReason

from .conftest import register, submit, wait_terminal


def test_health_and_meta(running_client):
    """健康检查与状态/失败原因枚举元信息"""
    client, _app = running_client
    assert client.get("/health").json() == {"status": "ok", "worker_running": True}
    meta = client.get("/meta/statuses").json()
    assert set(meta["delivery_status"]) == {"pending", "succeeded", "failed"}
    assert "signature_invalid" in meta["failure_reasons"]
    assert "unreachable" in meta["failure_reasons"]


def test_register_and_disable_endpoints(client, receiver_port):
    """注册/停用/启用接口的状态流转"""
    sub_id = register(client, "api-sub", "/hook/ok", "secret-ok-123", ["t.x"], receiver_port)
    listed = client.get("/subscribers").json()["subscribers"]
    assert len(listed) == 1 and listed[0]["id"] == sub_id

    disabled = client.post(f"/subscribers/{sub_id}/disable").json()
    assert disabled["status"] == "disabled"
    enabled = client.post(f"/subscribers/{sub_id}/enable").json()
    assert enabled["status"] == "active"


def test_submit_event_endpoint_and_delivery_query(running_client, receiver_port):
    """提交事件后异步成功送达，可查投递与尝试明细"""
    client, _app = running_client
    sub_id = register(client, "q-sub", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)
    result = submit(client, "evt-api-1", "order.created", {"hello": "world"})
    assert result["deduplicated"] is False
    assert len(result["deliveries"]) == 1
    delivery_id = result["deliveries"][0]["id"]

    deliveries = wait_terminal(client, "evt-api-1", expected=1)
    assert deliveries[0]["status"] == DeliveryStatus.SUCCEEDED.value

    detail = client.get(f"/deliveries/{delivery_id}").json()
    assert detail["subscriber_id"] == sub_id
    assert detail["attempts"] == 1
    assert detail["attempt_history"][0]["http_status"] == 200
    assert detail["failure_reason"] == FailureReason.NONE.value

    # 列表过滤
    assert len(client.get("/deliveries", params={"subscriber_id": sub_id}).json()["deliveries"]) == 1
    assert client.get("/deliveries/unknown").status_code == 404


def test_duplicate_submit_via_api_reports_deduplicated(client, receiver_port):
    """API 重复提交返回 deduplicated=true 且无新投递"""
    register(client, "d-sub", "/hook/ok", "secret-ok-123", ["order.created"], receiver_port)
    first = submit(client, "evt-api-dup", "order.created", {})
    second = submit(client, "evt-api-dup", "order.created", {})
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["deliveries"] == []


def test_invalid_event_payload_rejected(client):
    """非法事件请求体被 422 拒绝"""
    resp = client.post("/events", json={"event_id": "", "event_type": "x", "payload": {}})
    # Pydantic 字段约束与业务校验都应拒绝
    assert resp.status_code in (422,)


def test_event_not_found(client):
    """查询不存在事件返回 404"""
    assert client.get("/events/nope").status_code == 404
