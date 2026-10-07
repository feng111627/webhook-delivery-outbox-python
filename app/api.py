"""FastAPI HTTP 接口与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import Settings
from app.db import connect, init_db
from app.deliveryexecutor import DeliveryExecutor
from app.errors import InvalidEventError, InvalidSubscriberError, NotFoundError
from app.models import DeliveryStatus, FailureReason, SubscriberStatus
from app.service import WebhookService
from app.storage import OutboxStorage
from app.transport import HttpTransport
from app.worker import DeliveryWorker


# ---- 请求/响应模型 ----
class RegisterSubscriberIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str
    secret: str
    event_types: list[str]


class SubmitEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=128)
    payload: dict


def _subscriber_out(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "url": s.url,
        "event_types": s.event_types,
        "status": s.status.value,
        "created_at": s.created_at,
    }


def _delivery_out(d) -> dict:
    return {
        "id": d.id,
        "event_id": d.event_id,
        "subscriber_id": d.subscriber_id,
        "status": d.status.value,
        "attempts": d.attempts,
        "next_attempt_at": d.next_attempt_at,
        "failure_reason": d.failure_reason.value,
        "last_error": d.last_error,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def _attempt_out(a) -> dict:
    return {
        "attempt_no": a.attempt_no,
        "outcome": a.outcome.value,
        "http_status": a.http_status,
        "error_kind": a.error_kind,
        "receiver_code": a.receiver_code,
        "detail": a.detail,
        "started_at": a.started_at,
        "finished_at": a.finished_at,
    }


def create_app(
    settings: Settings | None = None,
    start_worker: bool = True,
    db_path: str | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    conn = connect(db_path or settings.database_path)
    init_db(conn)
    storage = OutboxStorage(conn)
    service = WebhookService(storage)
    transport = HttpTransport(timeout=settings.request_timeout_seconds)
    executor = DeliveryExecutor(storage, transport, settings)
    worker = DeliveryWorker(storage, executor, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop(timeout=5)
            conn.close()

    app = FastAPI(title="Webhook 投递服务", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.service = service
    app.state.worker = worker

    def get_service() -> WebhookService:
        return service

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "worker_running": worker.is_running()}

    # ---- 订阅方管理 ----
    @app.post("/subscribers", status_code=201)
    def register_subscriber(
        body: RegisterSubscriberIn, svc: WebhookService = Depends(get_service)
    ) -> dict:
        try:
            sub = svc.register_subscriber(body.name, body.url, body.secret, body.event_types)
        except InvalidSubscriberError as e:
            raise HTTPException(status_code=422, detail={"error": str(e)}) from e
        return _subscriber_out(sub)

    @app.get("/subscribers")
    def list_subscribers(svc: WebhookService = Depends(get_service)) -> dict:
        return {"subscribers": [_subscriber_out(s) for s in svc.list_subscribers()]}

    @app.post("/subscribers/{subscriber_id}/disable")
    def disable_subscriber(subscriber_id: str, svc: WebhookService = Depends(get_service)) -> dict:
        try:
            sub = svc.disable_subscriber(subscriber_id)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail={"error": str(e)}) from e
        return _subscriber_out(sub)

    @app.post("/subscribers/{subscriber_id}/enable")
    def enable_subscriber(subscriber_id: str, svc: WebhookService = Depends(get_service)) -> dict:
        try:
            sub = svc.enable_subscriber(subscriber_id)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail={"error": str(e)}) from e
        return _subscriber_out(sub)

    # ---- 事件提交 ----
    @app.post("/events", status_code=202)
    def submit_event(body: SubmitEventIn, svc: WebhookService = Depends(get_service)) -> dict:
        try:
            event, deliveries, created = svc.submit_event(
                body.event_id, body.event_type, body.payload
            )
        except InvalidEventError as e:
            raise HTTPException(status_code=422, detail={"error": str(e)}) from e
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "deduplicated": not created,
            "deliveries": [_delivery_out(d) for d in deliveries],
        }

    @app.get("/events/{event_id}")
    def get_event(event_id: str, svc: WebhookService = Depends(get_service)) -> dict:
        event = svc.get_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail={"error": "事件不存在"})
        deliveries = svc.list_deliveries(event_id=event_id)
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
            "deliveries": [_delivery_out(d) for d in deliveries],
        }

    # ---- 投递查询 ----
    @app.get("/deliveries")
    def list_deliveries(
        event_id: str | None = None,
        subscriber_id: str | None = None,
        svc: WebhookService = Depends(get_service),
    ) -> dict:
        deliveries = svc.list_deliveries(event_id=event_id, subscriber_id=subscriber_id)
        return {"deliveries": [_delivery_out(d) for d in deliveries]}

    @app.get("/deliveries/{delivery_id}")
    def get_delivery(delivery_id: str, svc: WebhookService = Depends(get_service)) -> dict:
        delivery, attempts = svc.get_delivery_detail(delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail={"error": "投递不存在"})
        out = _delivery_out(delivery)
        out["attempt_history"] = [_attempt_out(a) for a in attempts]
        return out

    # 状态/原因枚举说明，便于接入方查阅
    @app.get("/meta/statuses")
    def meta_statuses() -> dict:
        return {
            "delivery_status": [s.value for s in DeliveryStatus],
            "failure_reasons": [r.value for r in FailureReason if r is not FailureReason.NONE],
            "subscriber_status": [s.value for s in SubscriberStatus],
        }

    return app
