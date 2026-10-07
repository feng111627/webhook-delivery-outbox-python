"""本地模拟订阅方接收端。

用途：
- 校验 X-Webhook-Timestamp / X-Webhook-Signature，模拟真实接收方的验签行为；
- 通过路径前缀模拟多种投递结果，便于端到端验证重试/终态分类；
- 所有收到的事件记录在内存 STATE.received 中，供测试断言“到底送达了几次”。

路径约定（secrets 按路径最后一段或映射查）：
  POST /hook/ok        验签通过后返回 200
  POST /hook/500       始终 500（触发重试 -> retries_exhausted）
  POST /hook/slow?秒数  sleep 后返回 200（模拟慢订阅方），默认 2 秒
  POST /hook/badsig    无视请求，直接 401 error_code=signature_invalid
  POST /hook/oldskew   直接 401 error_code=timestamp_skew
  POST /hook/404       404 -> permanent_reject
  POST /hook/429       429 -> 可重试
  POST /hook/<name>    默认按验签结果返回 200/401
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.signing import (
    HEADER_EVENT,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    verify,
)

DEFAULT_TOLERANCE = 300


class ReceiverState:
    """跨请求共享的接收记录。"""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self._lock = threading.Lock()

    def record(self, item: dict) -> None:
        with self._lock:
            self.received.append(item)

    def events_for(self, event_id: str) -> list[dict]:
        with self._lock:
            return [r for r in self.received if r["event_id"] == event_id]

    def reset(self) -> None:
        with self._lock:
            self.received.clear()


STATE = ReceiverState()


def _resolve_secret(secrets: dict[str, str], path: str) -> str | None:
    # /hook/ok -> "ok"；也允许 secrets 用完整路径做键
    name = path.strip("/").split("/")[-1] if path.strip("/") else ""
    if path in secrets:
        return secrets[path]
    if name in secrets:
        return secrets[name]
    # 单一 secret 的便捷场景
    if len(secrets) == 1:
        return next(iter(secrets.values()))
    return None


def make_handler(
    secrets: dict[str, str],
    tolerance: int = DEFAULT_TOLERANCE,
    state: ReceiverState | None = None,
):
    state = state or STATE

    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "MockWebhook/1.0"

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 客户端超时先断开时静默处理
                pass

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            body = raw.decode("utf-8")

            event_id = self.headers.get(HEADER_EVENT, "")
            timestamp_raw = self.headers.get(HEADER_TIMESTAMP, "0")
            signature = self.headers.get(HEADER_SIGNATURE, "")

            # ---- 固定行为路径（不验签，用于触发各类投递结果）----
            if path.endswith("/500"):
                return self._reply(500, {"ok": False, "error": "server error"})
            if path.endswith("/404"):
                return self._reply(404, {"ok": False, "error": "not found"})
            if path.endswith("/429"):
                return self._reply(429, {"ok": False, "error": "rate limited"})
            if path.endswith("/badsig"):
                return self._reply(401, {"ok": False, "error_code": "signature_invalid"})
            if path.endswith("/oldskew"):
                return self._reply(401, {"ok": False, "error_code": "timestamp_skew"})
            if path.endswith("/slow"):
                delay = float(qs.get("delay", ["2"])[0])
                time.sleep(delay)
                state.record(
                    {
                        "event_id": event_id,
                        "path": path,
                        "body": body,
                        "received_at": time.time(),
                    }
                )
                return self._reply(200, {"ok": True})

            # ---- 正常路径：验签 + 时间戳窗口 ----
            secret = _resolve_secret(secrets, path)
            if secret is None:
                return self._reply(404, {"ok": False, "error": "unknown subscriber path"})
            try:
                timestamp = int(timestamp_raw)
            except ValueError:
                return self._reply(401, {"ok": False, "error_code": "timestamp_skew"})

            if abs(time.time() - timestamp) > tolerance:
                return self._reply(401, {"ok": False, "error_code": "timestamp_skew"})
            if not verify(secret, timestamp, event_id, body, signature):
                return self._reply(401, {"ok": False, "error_code": "signature_invalid"})

            state.record(
                {
                    "event_id": event_id,
                    "path": path,
                    "body": body,
                    "received_at": time.time(),
                }
            )
            self._reply(200, {"ok": True})

        def do_GET(self) -> None:  # noqa: N802
            # 便于人工确认服务在跑
            if urlparse(self.path).path == "/health":
                return self._reply(200, {"ok": True, "received": len(state.received)})
            self._reply(404, {"ok": False})

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            # 测试环境保持安静；演示时可覆盖
            return

    return WebhookHandler


def run_server(
    host: str = "127.0.0.1",
    port: int = 9100,
    secrets: dict[str, str] | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
    state: ReceiverState | None = None,
) -> ThreadingHTTPServer:
    secrets = secrets or {"ok": "local-secret-good"}
    state = state or STATE
    handler = make_handler(secrets, tolerance=tolerance, state=state)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="mock-receiver", daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="本地模拟 Webhook 接收端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument(
        "--secrets",
        default="ok=local-secret-good",
        help="逗号分隔的 name=secret 列表",
    )
    args = parser.parse_args()
    secret_map = {}
    for item in args.secrets.split(","):
        name, _, value = item.partition("=")
        secret_map[name.strip()] = value.strip()
    server = run_server(args.host, args.port, secret_map)
    print(f"模拟接收端已启动: http://{args.host}:{args.port} (paths: /hook/ok,/hook/500,/hook/slow,...)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
