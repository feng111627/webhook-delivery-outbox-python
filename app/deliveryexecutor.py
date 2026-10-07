"""把一条 due 投递真正送出去：发签名 HTTP、写尝试记录、按退避排程或判终态。

状态机：
  pending ──2xx──────────────────────> succeeded
  pending ──可重试错误且次数未满─────> pending(next_attempt_at 退避)
  pending ──可重试错误且次数已满─────> failed(retries_exhausted / unreachable)
  pending ──接收方明确 401 + 错误码──> failed(signature_invalid / timestamp_skew)
  pending ──其它 4xx─────────────────> failed(permanent_reject)
"""
from __future__ import annotations

import json
import logging
import time

from app.config import Settings
from app.models import (
    Attempt,
    AttemptOutcome,
    Delivery,
    FailureReason,
)
from app.signing import HEADER_EVENT, HEADER_SIGNATURE, HEADER_TIMESTAMP, sign
from app.storage import OutboxStorage
from app.transport import HttpTransport, TransportError, TransportErrorKind

logger = logging.getLogger("webhook.executor")

# 接收方在 401 响应体 {"error_code": ...} 中可能返回的约定错误码
RECEIVER_CODE_SIGNATURE_INVALID = "signature_invalid"
RECEIVER_CODE_TIMESTAMP_SKEW = "timestamp_skew"

# 可重试的状态码
RETRYABLE_STATUS = {408, 425, 429} | set(range(500, 600))


def backoff_delay(attempt_no: int, settings: Settings) -> float:
    """指数退避: base * 2^(n-1)，上限 max_backoff。attempt_no 从 1 开始。"""
    delay = settings.base_backoff_seconds * (2 ** (attempt_no - 1))
    return min(delay, settings.max_backoff_seconds)


def classify_response(
    status_code: int, receiver_code: str | None
) -> tuple[bool, bool, FailureReason | None]:
    """依据 HTTP 状态码与接收方错误码分类。

    返回 (是否成功, 是否终态, 失败原因)。
    """
    if 200 <= status_code < 300:
        return True, False, None
    if status_code == 401 and receiver_code == RECEIVER_CODE_SIGNATURE_INVALID:
        return False, True, FailureReason.SIGNATURE_INVALID
    if status_code == 401 and receiver_code == RECEIVER_CODE_TIMESTAMP_SKEW:
        return False, True, FailureReason.TIMESTAMP_SKEW
    if 400 <= status_code < 500 and status_code not in RETRYABLE_STATUS:
        return False, True, FailureReason.PERMANENT_REJECT
    # 5xx / 408 / 425 / 429 -> 可重试
    return False, False, FailureReason.RETRIES_EXHAUSTED


class DeliveryExecutor:
    def __init__(self, storage: OutboxStorage, transport: HttpTransport, settings: Settings) -> None:
        self.storage = storage
        self.transport = transport
        self.settings = settings

    def _load_event_payload(self, event_business_id: str) -> tuple[str, str]:
        event = self.storage.get_event(event_business_id)
        if event is None:
            raise ValueError(f"事件不存在: {event_business_id}")
        return event.event_type, json.dumps(event.payload, ensure_ascii=False)

    def run_delivery(self, delivery: Delivery) -> None:
        """执行一次投递尝试（成功/排程/终态都在这里落库）。"""
        now_wall = time.time()
        started = time.monotonic()

        subscriber = self.storage.get_subscriber(delivery.subscriber_id)
        if subscriber is None:
            self._finish_with_terminal_error(
                delivery, started, FailureReason.BAD_SUBSCRIBER, "订阅方不存在或已被删除"
            )
            return

        try:
            _event_type, body = self._load_event_payload(delivery.event_id)
        except ValueError as exc:
            self._finish_with_terminal_error(delivery, started, FailureReason.BAD_SUBSCRIBER, str(exc))
            return

        timestamp = int(now_wall)
        signature = sign(subscriber.secret, timestamp, delivery.event_id, body)
        headers = {
            HEADER_EVENT: delivery.event_id,
            HEADER_TIMESTAMP: str(timestamp),
            HEADER_SIGNATURE: signature,
            "Content-Type": "application/json",
        }

        attempt_no = delivery.attempts + 1
        attempt: Attempt
        try:
            result = self.transport.post(subscriber.url, body.encode("utf-8"), headers)
        except TransportError as exc:
            finished = time.monotonic()
            reason = (
                FailureReason.UNREACHABLE
                if exc.kind == TransportErrorKind.CONNECTION
                else FailureReason.RETRIES_EXHAUSTED
            )
            attempt = Attempt(
                delivery_id=delivery.id,
                attempt_no=attempt_no,
                started_at=started,
                finished_at=finished,
                outcome=AttemptOutcome.FAILURE,
                http_status=None,
                error_kind=exc.kind.value,
                receiver_code=None,
                detail=exc.message,
            )
            self._handle_failure(delivery, attempt, finished, reason, detail=exc.message)
            return

        finished = time.monotonic()
        ok, terminal, reason = classify_response(result.status_code, result.receiver_code())
        detail = f"HTTP {result.status_code}; body={result.body[:300]}"
        attempt = Attempt(
            delivery_id=delivery.id,
            attempt_no=attempt_no,
            started_at=started,
            finished_at=finished,
            outcome=AttemptOutcome.SUCCESS if ok else AttemptOutcome.FAILURE,
            http_status=result.status_code,
            error_kind=None if ok else "http_error",
            receiver_code=result.receiver_code(),
            detail=detail,
        )
        if ok:
            self.storage.mark_succeeded(delivery, attempt, finished)
            logger.info("投递成功 delivery=%s sub=%s event=%s", delivery.id, subscriber.id, delivery.event_id)
            return
        assert reason is not None
        self._handle_failure(delivery, attempt, finished, reason, detail=detail, terminal=terminal)

    def _handle_failure(
        self,
        delivery: Delivery,
        attempt: Attempt,
        now_mono: float,
        reason: FailureReason,
        detail: str,
        terminal: bool = False,
    ) -> None:
        now_wall = time.time()
        attempts_used = attempt.attempt_no
        if not terminal and attempts_used < self.settings.max_attempts:
            delay = backoff_delay(attempts_used, self.settings)
            next_at = now_wall + delay
            self.storage.record_attempt_and_schedule(delivery, attempt, now_wall, next_at)
            logger.info(
                "投递失败将重试 delivery=%s attempt=%s/%s delay=%.2fs reason=%s detail=%s",
                delivery.id, attempts_used, self.settings.max_attempts, delay, reason.value, detail,
            )
            return
        # 终态：显式终态错误（签名/时间戳/4xx）或重试类错误（5xx/超时/不可达）已耗尽次数。
        # 每次尝试的具体类别记录在 delivery_attempts.error_kind，可事后追溯。
        self.storage.mark_failed(delivery, attempt, reason, now_wall)
        logger.warning(
            "投递进入终态 delivery=%s attempts=%s reason=%s detail=%s",
            delivery.id, attempts_used, reason.value, detail,
        )

    def _finish_with_terminal_error(
        self, delivery: Delivery, started_mono: float, reason: FailureReason, detail: str
    ) -> None:
        finished = time.monotonic()
        attempt = Attempt(
            delivery_id=delivery.id,
            attempt_no=delivery.attempts + 1,
            started_at=started_mono,
            finished_at=finished,
            outcome=AttemptOutcome.FAILURE,
            http_status=None,
            error_kind="configuration",
            receiver_code=None,
            detail=detail,
        )
        self.storage.mark_failed(delivery, attempt, reason, time.time())
