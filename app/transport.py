"""HTTP 投递传输层。基于标准库 urllib，本地模拟接收端即可验证，不依赖外部服务。"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum


class TransportErrorKind(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BAD_RESPONSE = "bad_response"
    UNKNOWN = "unknown"


class TransportError(Exception):
    def __init__(self, kind: TransportErrorKind, message: str, elapsed: float) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.elapsed = elapsed


@dataclass
class TransportResult:
    status_code: int
    body: str
    headers: dict[str, str]
    elapsed: float

    def receiver_code(self) -> str | None:
        """接收方约定用 JSON body {"error_code": "..."} 表达鉴权/时间戳失败。"""
        if not self.body:
            return None
        try:
            data = json.loads(self.body)
        except (ValueError, TypeError):
            return None
        if isinstance(data, dict):
            code = data.get("error_code")
            return code if isinstance(code, str) else None
        return None


class HttpTransport:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> TransportResult:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)

        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                elapsed = time.monotonic() - started
                return TransportResult(
                    status_code=resp.status,
                    body=raw.decode("utf-8", errors="replace"),
                    headers={k: v for k, v in resp.headers.items()},
                    elapsed=elapsed,
                )
        except urllib.error.HTTPError as e:
            # 对方有响应但状态码非 2xx，仍然算得到了响应
            elapsed = time.monotonic() - started
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            return TransportResult(
                status_code=e.code,
                body=raw.decode("utf-8", errors="replace"),
                headers={k: v for k, v in (e.headers.items() if e.headers else [])},
                elapsed=elapsed,
            )
        except (socket.timeout, TimeoutError) as e:
            raise TransportError(
                TransportErrorKind.TIMEOUT, f"请求超时: {e}", time.monotonic() - started
            ) from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
                kind = TransportErrorKind.TIMEOUT
            else:
                kind = TransportErrorKind.CONNECTION
            raise TransportError(kind, f"连接失败: {reason}", time.monotonic() - started) from e
