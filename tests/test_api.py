"""HTTP API 层测试：参数校验、启停、查询。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from webhook.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"), start_worker=False)
    with TestClient(app) as c:
        yield c


def _register(client, **overrides):
    body = {
        "name": "sub-1",
        "url": "http://127.0.0.1:9100/hook/ok",
        "secret": "receiver-local-secret-0123456789",
        "event_types": ["order.created"],
    }
    body.update(overrides)
    return client.post("/api/v1/subscriptions", json=body)


def test_register_and_list(client):
    r = _register(client)
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["status"] == "active"

    r = client.get("/api/v1/subscriptions")
    assert r.status_code == 200
    assert len(r.json()["subscriptions"]) == 1


def test_duplicate_name_rejected(client):
    assert _register(client).status_code == 201
    r = _register(client, url="http://127.0.0.1:9100/hook/other")
    assert r.status_code == 422
    assert "已存在" in r.json()["detail"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "ftp://127.0.0.1/x"),
        ("url", "http://"),
        ("url", ""),
        ("secret", "short"),
        ("secret", "secretsecret1234"),
        ("event_types", []),
        ("event_types", [""]),
        ("name", ""),
    ],
)
def test_invalid_config_rejected(client, field, value):
    r = _register(client, **{field: value})
    assert r.status_code == 422, (field, r.status_code, r.text)


def test_enable_disable_flow(client):
    sub = _register(client).json()
    sid = sub["id"]
    r = client.post(f"/api/v1/subscriptions/{sid}/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = client.post(f"/api/v1/subscriptions/{sid}/enabled", json={"enabled": True})
    assert r.json()["status"] == "active"

    r = client.post("/api/v1/subscriptions/999/enabled", json={"enabled": True})
    assert r.status_code == 404


def test_event_submit_and_status(client):
    _register(client)
    r1 = client.post(
        "/api/v1/events",
        json={"event_id": "ev1", "event_type": "order.created", "payload": {"a": 1}},
    )
    assert r1.status_code == 202
    assert r1.json()["matched_subscriptions"] == 1

    r2 = client.post(
        "/api/v1/events",
        json={"event_id": "ev1", "event_type": "order.created", "payload": {"a": 2}},
    )
    assert r2.status_code == 200 and r2.json()["deduped"] is True

    r = client.get("/api/v1/events/ev1")
    assert r.status_code == 200
    assert len(r.json()["deliveries"]) == 1
    delivery_id = r.json()["deliveries"][0]["delivery_id"]

    r = client.get(f"/api/v1/deliveries/{delivery_id}")
    assert r.status_code == 200
    assert r.json()["attempts"] == [] or isinstance(r.json()["attempts"], list)

    assert client.get("/api/v1/events/nope").status_code == 404
    assert client.get("/api/v1/deliveries/999").status_code == 404
