"""应用装配：构建 FastAPI 应用，lifespan 中启停后台 worker。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from webhook.api import create_router
from webhook.config import Settings
from webhook.db import connect, init_db
from webhook.delivery import WebhookDeliverer
from webhook.repositories import (
    DeliveryRepository,
    EventRepository,
    SubscriptionRepository,
)
from webhook.service import WebhookService
from webhook.worker import DeliveryWorker


def build_components(database_path: str, settings: Settings | None = None):
    settings = settings or Settings(database_path=database_path)
    conn = connect(database_path)
    init_db(conn)
    subs = SubscriptionRepository(conn)
    events = EventRepository(conn)
    deliveries = DeliveryRepository(conn)
    service = WebhookService(subs, events, deliveries)
    deliverer = WebhookDeliverer(timeout=settings.http_timeout)
    worker = DeliveryWorker(subs, events, deliveries, deliverer, settings)
    return service, worker, deliverer, conn


def create_app(
    database_path: str = "data/webhook.db",
    start_worker: bool = True,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings(database_path=database_path)
    service, worker, deliverer, conn = build_components(database_path, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            await worker.start()
        try:
            yield
        finally:
            if start_worker:
                await worker.stop()
            conn.close()

    app = FastAPI(
        title="可靠 Webhook 投递服务",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.worker = worker
    app.state.deliverer = deliverer
    app.state.conn = conn

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(create_router(service))
    return app
