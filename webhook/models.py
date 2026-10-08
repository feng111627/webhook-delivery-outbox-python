"""领域模型与状态枚举。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED_RETRIES_EXHAUSTED = "failed_retries_exhausted"
    FAILED_SIGNATURE_REJECTED = "failed_signature_rejected"
    FAILED_SUBSCRIBER_UNREACHABLE = "failed_subscriber_unreachable"
    SKIPPED_SUBSCRIBER_DISABLED = "skipped_subscriber_disabled"


class FailureReason(str, enum.Enum):
    NONE = "none"
    NON_2XX = "non_2xx"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SIGNATURE_REJECTED = "signature_rejected"
    UNREACHABLE = "unreachable"


@dataclass
class Subscription:
    id: int
    name: str
    url: str
    secret: str
    event_types: list[str]
    status: SubscriptionStatus
    created_at: datetime
    updated_at: datetime


@dataclass
class Event:
    id: int
    event_id: str
    event_type: str
    payload: str
    created_at: datetime


@dataclass
class Delivery:
    id: int
    event_id: int
    subscription_id: int
    status: DeliveryStatus
    attempts: int
    next_attempt_after: datetime | None
    last_failure_reason: FailureReason
    last_response_code: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
