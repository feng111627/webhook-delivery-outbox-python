"""本地模拟订阅方接收端。

用途：不依赖任何外部服务，在本机起一个能验证签名、模拟各种下游行为
（慢响应、5xx、401 拒签等）的 HTTP 服务，供投递服务真实地发请求。

行为按路径区分：

* ``/hook/ok``        -> 校验通过后返回 200
* ``/hook/slow``      -> sleep ``X-Simulate-Delay``（默认 3 秒）后 200
* ``/hook/fail``      -> 固定 503
* ``/hook/flaky``     -> 同一 event_id 前两次 503，第三次起 200
* ``/hook/badsecret`` -> 该路径使用另一个密钥，签名恒不通过 -> 401

所有路径都先校验 X-Webhook-Timestamp 新鲜度与 X-Webhook-Signature，
失败时返回 401 并带 ``X-Webhook-Error`` 头，投递端据此直接判为
``failed_signature_rejected`` 终态（不会无意义重试）。
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook.signing import timestamp_is_fresh, verify

FLAKY_FAIL_TIMES = 2


def create_receiver_app(
    secret: str = "receiver-local-secret-0123456789",
    wrong_secret: str = "receiver-other-secret-9876543210",
    timestamp_skew_seconds: int = 300,
) -> FastAPI:
    flaky_counts: dict[tuple[str, str], int] = {}
    received: list[dict] = []

    async def _handle(request: Request) -> JSONResponse:
        body = await request.body()
        path = request.url.path
        expected_secret = wrong_secret if path.endswith("/badsecret") else secret
        ts = request.headers.get("x-webhook-timestamp", "")
        sig = request.headers.get("x-webhook-signature", "")

        if not timestamp_is_fresh(ts, timestamp_skew_seconds):
            return JSONResponse(
                status_code=401,
                content={"received": False, "message": "invalid_timestamp"},
                headers={"x-webhook-error": "invalid_timestamp"},
            )
        if not verify(expected_secret, ts, body, sig):
            return JSONResponse(
                status_code=401,
                content={"received": False, "message": "invalid_signature"},
                headers={"x-webhook-error": "invalid_signature"},
            )

        payload = await request.json()
        event_id = request.headers.get("x-webhook-id", payload.get("event_id", ""))

        if path.endswith("/slow"):
            delay = float(request.headers.get("x-simulate-delay", "3"))
            await asyncio.sleep(max(0.0, delay))
            received.append(
                {"path": path, "event_id": event_id, "body": payload, "delivered": True}
            )
            return JSONResponse({"received": True, "message": "slow-ok"})

        if path.endswith("/fail"):
            return JSONResponse(
                status_code=503,
                content={"received": False, "message": "permanent-fail"},
            )

        if path.endswith("/flaky"):
            key = (path, event_id)
            count = flaky_counts.get(key, 0)
            flaky_counts[key] = count + 1
            if count < FLAKY_FAIL_TIMES:
                return JSONResponse(
                    status_code=503,
                    content={"received": False, "message": f"flaky-fail-{count + 1}"},
                )
            received.append(
                {"path": path, "event_id": event_id, "body": payload, "delivered": True}
            )
            return JSONResponse({"received": True, "message": "flaky-ok"})

        received.append(
            {"path": path, "event_id": event_id, "body": payload, "delivered": True}
        )
        return JSONResponse({"received": True, "message": "ok"})

    app = FastAPI(title="本地模拟 Webhook 接收端")
    app.add_api_route(
        "/hook/{name:path}", _handle, methods=["POST"], include_in_schema=False
    )

    @app.get("/received")
    def list_received():
        return {"count": len(received), "events": received}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.state.received = received
    return app
