"""HTTP 投递客户端：发送 POST 并把结果归类成明确的失败原因。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

from webhook.signing import sign

# 接收端通过专用响应头/状态码告知"签名或时间戳校验失败"：
#   HTTP 401 且 X-Webhook-Error: invalid_signature | invalid_timestamp
SIGNATURE_REJECTION_STATUS = 401
SIGNATURE_ERROR_HEADER = "x-webhook-error"


@dataclass
class DeliveryResult:
    success: bool
    status_code: int | None
    failure_reason: str  # webhook.models.FailureReason 的值
    error: str | None
    response_excerpt: str | None = None
    http_duration_ms: float = 0.0


class WebhookDeliverer:
    """无状态投递器；httpx 客户端可注入，便于单测打桩。"""

    def __init__(
        self,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        time_func=time.time,
    ) -> None:
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._time_func = time_func

    async def __aenter__(self) -> "WebhookDeliverer":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def deliver(
        self,
        url: str,
        secret: str,
        event_id: str,
        event_type: str,
        payload: str,
        timestamp: str | None = None,
    ) -> DeliveryResult:
        ts = timestamp if timestamp is not None else str(int(self._time_func()))
        body = json.dumps(
            {"event_id": event_id, "event_type": event_type, "payload": json.loads(payload)},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        signature = sign(secret, ts, body)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "solo-webhook-delivery/0.1",
            "X-Webhook-Id": event_id,
            "X-Webhook-Event": event_type,
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": signature,
        }
        client = self._client
        if client is None:
            # 未使用 async context 时按需自建客户端
            client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        started = self._time_func()
        try:
            resp = await client.post(url, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            return DeliveryResult(
                success=False,
                status_code=None,
                failure_reason="timeout",
                error=f"{type(exc).__name__}: {exc}",
                http_duration_ms=(self._time_func() - started) * 1000,
            )
        except httpx.TransportError as exc:
            return DeliveryResult(
                success=False,
                status_code=None,
                failure_reason="connection_error",
                error=f"{type(exc).__name__}: {exc}",
                http_duration_ms=(self._time_func() - started) * 1000,
            )
        duration_ms = (self._time_func() - started) * 1000
        if 200 <= resp.status_code < 300:
            return DeliveryResult(
                success=True,
                status_code=resp.status_code,
                failure_reason="none",
                error=None,
                response_excerpt=_excerpt(resp.text),
                http_duration_ms=duration_ms,
            )
        if (
            resp.status_code == SIGNATURE_REJECTION_STATUS
            and resp.headers.get(SIGNATURE_ERROR_HEADER) in ("invalid_signature", "invalid_timestamp")
        ):
            reason = "signature_rejected"
        else:
            reason = "non_2xx"
        return DeliveryResult(
            success=False,
            status_code=resp.status_code,
            failure_reason=reason,
            error=None,
            response_excerpt=_excerpt(resp.text),
            http_duration_ms=duration_ms,
        )


def _excerpt(text: str, limit: int = 500) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "...(truncated)"
