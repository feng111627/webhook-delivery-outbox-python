"""应用服务层：订阅方管理、事件提交、投递状态查询。

事件提交是同步写库、异步投递：业务侧调用 ``submit_event`` 只会做
本地 SQLite 写入（毫秒级）后立即返回；真正的 HTTP 发送由后台 worker
完成。所有去重都依赖数据库唯一约束，重复提交/并发提交都安全。
"""
from __future__ import annotations

import json

from webhook.errors import NotFoundError, ValidationError
from webhook.models import DeliveryStatus
from webhook.repositories import (
    DeliveryRepository,
    EventRepository,
    SubscriptionRepository,
)
from webhook.validation import (
    validate_event_types,
    validate_name,
    validate_secret,
    validate_url,
)

# 给外部查看状态时的终态/进行中归类
_TERMINAL_STATUSES = {
    DeliveryStatus.SUCCEEDED,
    DeliveryStatus.FAILED_RETRIES_EXHAUSTED,
    DeliveryStatus.FAILED_SIGNATURE_REJECTED,
    DeliveryStatus.FAILED_SUBSCRIBER_UNREACHABLE,
    DeliveryStatus.SKIPPED_SUBSCRIBER_DISABLED,
}


class WebhookService:
    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        events: EventRepository,
        deliveries: DeliveryRepository,
    ) -> None:
        self.subscriptions = subscriptions
        self.events = events
        self.deliveries = deliveries

    # ---- 订阅方管理 -------------------------------------------------

    def register_subscription(
        self, name: str, url: str, secret: str, event_types: list[str]
    ) -> dict:
        name = validate_name(name)
        url = validate_url(url)
        secret = validate_secret(secret)
        event_types = validate_event_types(event_types)
        if self.subscriptions.get_by_name(name) is not None:
            raise ValidationError(f"订阅方名称已存在: {name}")
        sub = self.subscriptions.create(name, url, secret, event_types)
        return _subscription_dict(sub)

    def list_subscriptions(self) -> list[dict]:
        return [_subscription_dict(s) for s in self.subscriptions.list_all()]

    def get_subscription(self, subscription_id: int) -> dict:
        sub = self.subscriptions.get(subscription_id)
        if sub is None:
            raise NotFoundError(f"订阅方不存在: {subscription_id}")
        return _subscription_dict(sub)

    def set_subscription_enabled(self, subscription_id: int, enabled: bool) -> dict:
        sub = self.subscriptions.get(subscription_id)
        if sub is None:
            raise NotFoundError(f"订阅方不存在: {subscription_id}")
        if not enabled:
            # 停用：尚未成功的投递直接进入跳过终态，不再占用投递资源
            self.deliveries.skip_pending_for_subscription(subscription_id)
        sub = self.subscriptions.set_status(subscription_id, enabled)
        return _subscription_dict(sub)

    # ---- 事件提交 ---------------------------------------------------

    def submit_event(self, event_id: str, event_type: str, payload: dict) -> dict:
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValidationError("event_id 不能为空")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValidationError("event_type 不能为空")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是 JSON 对象")
        try:
            payload_text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"payload 不是合法 JSON: {exc}") from exc

        event, event_created = self.events.create_if_absent(
            event_id.strip(), event_type.strip(), payload_text
        )
        targets = self.subscriptions.list_active_for_event(event.event_type)
        created_deliveries: list[int] = []
        for sub in targets:
            delivery, created = self.deliveries.create_if_absent(event.id, sub.id)
            if created and delivery is not None:
                created_deliveries.append(delivery.id)
        return {
            "event_id": event.event_id,
            "deduped": not event_created,
            "matched_subscriptions": len(targets),
            "new_deliveries": created_deliveries,
        }

    # ---- 状态查询 ---------------------------------------------------

    def get_event_status(self, event_id: str) -> dict:
        event = self.events.get(event_id)
        if event is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        deliveries = self.deliveries.list_for_event(event.id)
        items = []
        for d in deliveries:
            sub = self.subscriptions.get(d.subscription_id)
            items.append(
                {
                    "delivery_id": d.id,
                    "subscription_id": d.subscription_id,
                    "subscription_name": sub.name if sub else None,
                    "status": d.status.value,
                    "terminal": d.status in _TERMINAL_STATUSES,
                    "attempts": d.attempts,
                    "last_failure_reason": d.last_failure_reason.value,
                    "last_response_code": d.last_response_code,
                    "last_error": d.last_error,
                    "next_attempt_after": d.next_attempt_after.isoformat()
                    if d.next_attempt_after
                    else None,
                }
            )
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "deliveries": items,
        }

    def get_delivery_detail(self, delivery_id: int) -> dict:
        d = self.deliveries.get(delivery_id)
        if d is None:
            raise NotFoundError(f"投递记录不存在: {delivery_id}")
        sub = self.subscriptions.get(d.subscription_id)
        event = self.events.get_by_pk(d.event_id)
        return {
            "delivery_id": d.id,
            "subscription": _subscription_dict(sub) if sub else None,
            "event_id": event.event_id if event else None,
            "status": d.status.value,
            "attempts_count": d.attempts,
            "last_failure_reason": d.last_failure_reason.value,
            "last_response_code": d.last_response_code,
            "last_error": d.last_error,
            "attempts": self.deliveries.list_attempts(d.id),
        }


def _subscription_dict(sub) -> dict:
    return {
        "id": sub.id,
        "name": sub.name,
        "url": sub.url,
        "event_types": sub.event_types,
        "status": sub.status.value,
        "created_at": sub.created_at.isoformat(),
        "updated_at": sub.updated_at.isoformat(),
    }
