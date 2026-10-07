"""pytest 公共夹具：内存库、快节奏配置、本地模拟接收端、API 客户端。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.db import connect, init_db
from app.service import WebhookService
from app.storage import OutboxStorage
from app.transport import HttpTransport
from app.deliveryexecutor import DeliveryExecutor
from app.worker import DeliveryWorker
from tools.mock_receiver import ReceiverState, run_server


def fast_settings(**overrides) -> Settings:
    base = dict(
        max_attempts=4,
        base_backoff_seconds=0.02,
        max_backoff_seconds=0.2,
        request_timeout_seconds=5.0,
        lock_seconds=5.0,
        poll_interval_seconds=0.01,
        worker_threads=8,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return fast_settings()


@pytest.fixture
def storage():
    conn = connect(":memory:")
    init_db(conn)
    yield OutboxStorage(conn)
    conn.close()


@pytest.fixture
def service(storage) -> WebhookService:
    return WebhookService(storage)


@pytest.fixture
def transport(settings) -> HttpTransport:
    return HttpTransport(timeout=settings.request_timeout_seconds)


@pytest.fixture
def executor(storage, transport, settings) -> DeliveryExecutor:
    return DeliveryExecutor(storage, transport, settings)


@pytest.fixture
def receiver_state() -> ReceiverState:
    state = ReceiverState()
    return state


@pytest.fixture
def receiver_server(receiver_state):
    """启动本地模拟接收端，测试结束关闭。"""
    server = run_server(
        host="127.0.0.1",
        port=0,
        secrets={"ok": "secret-ok-123", "slow": "secret-slow-123"},
        state=receiver_state,
    )
    yield server, receiver_state
    server.shutdown()


@pytest.fixture
def receiver_port(receiver_server):
    server, _state = receiver_server
    return server.server_address[1]


@pytest.fixture
def client(settings, storage):
    """不带后台 worker 的 API 客户端（接口逻辑测试用）。"""
    app = create_app(settings=settings, start_worker=False, db_path=":memory:")
    # 复用 fixture 里的 storage：直接替换装配好的实例
    app.state.storage = storage
    app.state.service = WebhookService(storage)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def running_client(settings):
    """带后台 worker 的 API 客户端 + 服务句柄，用于端到端异步投递测试。"""
    app = create_app(settings=settings, start_worker=True, db_path=":memory:")
    with TestClient(app) as c:
        yield c, app


def register(client, name: str, path: str, secret: str, event_types: list[str], port: int) -> str:
    resp = client.post(
        "/subscribers",
        json={
            "name": name,
            "url": f"http://127.0.0.1:{port}{path}",
            "secret": secret,
            "event_types": event_types,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def submit(client, event_id: str, event_type: str, payload: dict) -> dict:
    resp = client.post(
        "/events",
        json={"event_id": event_id, "event_type": event_type, "payload": payload},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def wait_terminal(client, event_id: str, expected: int, timeout: float = 10.0) -> list[dict]:
    """等待事件的投递全部离开 pending。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/events/{event_id}")
        assert resp.status_code == 200
        deliveries = resp.json()["deliveries"]
        if len(deliveries) >= expected and all(d["status"] != "pending" for d in deliveries):
            return deliveries
        time.sleep(0.02)
    raise AssertionError(f"投递未在 {timeout}s 内全部结束: {event_id}")


def delivery_body(event_id: str, payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---- 运行日志：打印每个用例覆盖的业务场景与通过情况 ----
def pytest_report_header(config):
    return [
        "Webhook 投递子系统 - 业务场景测试套件",
        "覆盖: 幂等去重 / 并发提交 / 慢订阅方隔离 / 退避重试到终态 /",
        "      失败原因分类(不可达·签名·时间戳) / 停用恢复 / 配置校验 / HTTP API",
    ]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when in ("call", "setup"):
        doc = (item.function.__doc__ or "").strip().splitlines()
        report._scenario_doc = doc[0] if doc else item.name
        report._scenario_file = item.nodeid.split("::")[0]


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tr = terminalreporter
    tr.write_sep("=", "业务场景覆盖结果", bold=True)
    groups: dict[str, list] = {}
    for key in ("passed", "failed", "error"):
        for r in tr.stats.get(key, []):
            if hasattr(r, "_scenario_file"):
                groups.setdefault(r._scenario_file, []).append(r)
    title_map = {
        "tests/test_signing.py": "签名协议（HMAC-SHA256，篡改可检出）",
        "tests/test_idempotency.py": "重复提交 / 并发重复事件不重复投递",
        "tests/test_slow_subscriber.py": "慢订阅方不阻塞其它订阅方与后续事件",
        "tests/test_retry.py": "退避重试到终态与失败原因分类",
        "tests/test_subscriber_lifecycle.py": "订阅方停用/恢复与非法配置拒绝",
        "tests/test_api.py": "HTTP API 端到端",
    }
    for fname, reps in groups.items():
        passed = sum(1 for r in reps if r.passed)
        tr.write_line(f"[{passed}/{len(reps)} 通过] {title_map.get(fname, fname)}")
        for r in reps:
            tr.write_line(f"    {'PASS' if r.passed else 'FAIL'}  {r._scenario_doc}")
