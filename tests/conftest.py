"""端到端测试夹具：本机真实 TCP 上启动模拟接收端与主服务。"""
from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn

from webhook.app import build_components
from webhook.config import Settings
from webhook.receiver import create_receiver_app


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=str(tmp_path / "e2e.db"),
        poll_interval=0.05,
        http_timeout=1.0,
        max_attempts=4,
        base_delay=0.05,
        max_delay=0.2,
        timestamp_skew_seconds=300,
    )


async def _wait_port(port: int, timeout: float = 5.0) -> None:
    import httpx

    deadline = asyncio.get_running_loop().time() + timeout
    last_exc = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/health")
                if r.status_code == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        await asyncio.sleep(0.05)
    raise RuntimeError(f"server on {port} did not start: {last_exc}")


@pytest.fixture
async def receiver_base():
    port = free_port()
    config = uvicorn.Config(
        create_receiver_app(),
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await _wait_port(port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def components(settings):
    """不启动 HTTP server 的应用组件，直接驱动 worker（快、确定性高）。"""
    service, worker, deliverer, conn = build_components(
        settings.database_path, settings
    )
    async with deliverer:
        await worker.start()
        try:
            yield service, worker, deliverer
        finally:
            await worker.stop()
            conn.close()


async def wait_for(condition, timeout: float = 10.0, interval: float = 0.05):
    """轮询等待条件成立，condition 可以是普通函数或协程函数。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met before timeout")
