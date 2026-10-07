"""领域模型与状态/原因枚举（仅数据定义）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SubscriberStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureReason(str, Enum):
    """投递终态失败原因，可被查询方明确区分。"""

    NONE = "none"
    # 可重试类错误（5xx / 408 / 429 / 超时）在达到重试上限后进入终态
    RETRIES_EXHAUSTED = "retries_exhausted"
    # 连接层面始终无法建立（拒绝连接 / DNS 等），重试耗尽
    UNREACHABLE = "unreachable"
    # 接收方明确判定签名无效
    SIGNATURE_INVALID = "signature_invalid"
    # 接收方明确判定时间戳超出容忍窗口
    TIMESTAMP_SKEW = "timestamp_skew"
    # 接收方以其它 4xx 永久拒绝
    PERMANENT_REJECT = "permanent_reject"
    # 订阅方配置无效（如 URL 已不可用的兜底）
    BAD_SUBSCRIBER = "bad_subscriber"


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class Subscriber:
    id: str
    name: str
    url: str
    secret: str
    event_types: list[str]
    status: SubscriberStatus = SubscriberStatus.ACTIVE
    created_at: float = 0.0


@dataclass
class Event:
    id: int
    event_id: str
    event_type: str
    payload: dict
    created_at: float


@dataclass
class Delivery:
    id: str
    event_id: str
    subscriber_id: str
    status: DeliveryStatus
    attempts: int = 0
    next_attempt_at: float = 0.0
    locked_until: float | None = None
    failure_reason: FailureReason = FailureReason.NONE
    last_error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class Attempt:
    delivery_id: str
    attempt_no: int
    started_at: float
    finished_at: float
    outcome: AttemptOutcome
    http_status: int | None = None
    error_kind: str | None = None
    receiver_code: str | None = None
    detail: str | None = None
    id: int | None = None
