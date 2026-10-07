"""应用服务层：订阅方管理与事件提交。"""
from __future__ import annotations

import time
import uuid

from app.errors import InvalidSubscriberError, NotFoundError
from app.models import Attempt, Delivery, Event, Subscriber, SubscriberStatus
from app.storage import OutboxStorage
from app.validation import (
    validate_event,
    validate_event_types,
    validate_secret,
    validate_url,
)


class WebhookService:
    def __init__(self, storage: OutboxStorage) -> None:
        self.storage = storage

    # ---- 订阅方管理 ----
    def register_subscriber(
        self, name: str, url: str, secret: str, event_types: list[str]
    ) -> Subscriber:
        if not isinstance(name, str) or not name.strip():
            raise InvalidSubscriberError("name 必须是非空字符串")
        url = validate_url(url)
        secret = validate_secret(secret)
        event_types = validate_event_types(event_types)
        subscriber = Subscriber(
            id=f"sub_{uuid.uuid4().hex}",
            name=name.strip(),
            url=url,
            secret=secret,
            event_types=event_types,
            status=SubscriberStatus.ACTIVE,
            created_at=time.time(),
        )
        return self.storage.create_subscriber(subscriber)

    def disable_subscriber(self, subscriber_id: str) -> Subscriber:
        if not self.storage.set_subscriber_status(subscriber_id, active=False):
            raise NotFoundError(f"订阅方不存在: {subscriber_id}")
        subscriber = self.storage.get_subscriber(subscriber_id)
        assert subscriber is not None
        return subscriber

    def enable_subscriber(self, subscriber_id: str) -> Subscriber:
        if not self.storage.set_subscriber_status(subscriber_id, active=True):
            raise NotFoundError(f"订阅方不存在: {subscriber_id}")
        subscriber = self.storage.get_subscriber(subscriber_id)
        assert subscriber is not None
        return subscriber

    def list_subscribers(self) -> list[Subscriber]:
        return self.storage.list_subscribers(include_disabled=True)

    def get_subscriber(self, subscriber_id: str) -> Subscriber | None:
        return self.storage.get_subscriber(subscriber_id)

    # ---- 事件 ----
    def submit_event(
        self, event_id: str, event_type: str, payload: dict
    ) -> tuple[Event, list[Delivery], bool]:
        """提交事件并为所有匹配的启用订阅方创建投递（Outbox 扇出）。

        幂等：event_id 重复提交时返回已存在事件、空投递列表，绝不重复创建投递。
        返回 (事件, 新建的投递列表, 是否首次提交)。
        """
        validate_event(event_id, payload)
        event, created = self.storage.insert_event(event_id, event_type, payload)
        if not created:
            return event, [], False

        subscribers = self.storage.list_active_subscribers_for_event(event_type)
        deliveries = self.storage.create_deliveries(
            event_id, [s.id for s in subscribers], time.time()
        )
        return event, deliveries, True

    # ---- 查询 ----
    def get_event(self, event_id: str) -> Event | None:
        return self.storage.get_event(event_id)

    def list_deliveries(
        self, event_id: str | None = None, subscriber_id: str | None = None
    ) -> list[Delivery]:
        return self.storage.list_deliveries(event_id=event_id, subscriber_id=subscriber_id)

    def get_delivery_detail(self, delivery_id: str) -> tuple[Delivery | None, list[Attempt]]:
        delivery = self.storage.get_delivery(delivery_id)
        if delivery is None:
            return None, []
        return delivery, self.storage.list_attempts(delivery_id)
