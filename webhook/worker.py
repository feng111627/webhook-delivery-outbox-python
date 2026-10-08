"""后台投递 worker。

关键性质：

* 每条到期记录由 ``claim_due`` 原子抢占（pending/retrying -> in_flight），
  并发扫描 / 多 worker 也不会重复发送。
* 每条投递独立 ``asyncio.Task``：某个订阅方响应很慢只会挂起自己的
  任务，不阻塞其它订阅方与后续事件。
* 退避重试：失败后 attempts+1，按指数退避计算 next_attempt_after；
  超过 max_attempts 进入不同终态，失败原因彼此可区分：

  - ``failed_retries_exhausted``：收到过 HTTP 响应但一直非 2xx
    （至少响应过，对端可达）；
  - ``failed_signature_rejected``：接收端明确以 401 + 专用头拒绝
    签名/时间戳，不重试，直接终态；
  - ``failed_subscriber_unreachable``：全部尝试均为超时/连接失败，
    一次响应都没拿到。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from webhook.retry import backoff_delay
from webhook.repositories import (
    DeliveryRepository,
    EventRepository,
    SubscriptionRepository,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryWorker:
    def __init__(
        self,
        subscriptions: SubscriptionRepository,
        events: EventRepository,
        deliveries: DeliveryRepository,
        deliverer,
        settings,
    ) -> None:
        self.subscriptions = subscriptions
        self.events = events
        self.deliveries = deliveries
        self.deliverer = deliverer
        self.settings = settings
        self._task: asyncio.Task | None = None
        self._in_flight: set[asyncio.Task] = set()

    async def start(self) -> None:
        # 上次进程残留的 in_flight 退回 pending，避免卡死
        self.deliveries.requeue_in_flight()
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.process_due(wait=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 循环不能因异常退出
                pass
            await asyncio.sleep(self.settings.poll_interval)

    async def run_once(self) -> int:
        """扫描并把本批到期投递全部处理完（含落库），返回投递条数。

        会等待任务结束，便于单测做确定性断言；后台循环使用 wait=False。
        """
        tasks = await self.process_due(wait=True)
        return len(tasks)

    async def process_due(self, wait: bool) -> list[asyncio.Task]:
        now = utcnow()
        claimed = self.deliveries.claim_due(now.isoformat())
        tasks: list[asyncio.Task] = []
        for delivery in claimed:
            task = asyncio.create_task(self._process(delivery.id))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            tasks.append(task)
        if wait and tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return tasks

    async def _process(self, delivery_id: int) -> None:
        delivery = self.deliveries.get(delivery_id)
        if delivery is None:  # pragma: no cover
            return
        event = self.events.get_by_pk(delivery.event_id)
        sub = self.subscriptions.get(delivery.subscription_id)
        if event is None or sub is None:  # pragma: no cover
            return
        result = await self.deliverer.deliver(
            url=sub.url,
            secret=sub.secret,
            event_id=event.event_id,
            event_type=event.event_type,
            payload=event.payload,
        )
        attempts = delivery.attempts + 1
        self.deliveries.add_attempt(
            delivery_id=delivery_id,
            attempt_no=attempts,
            status_code=result.status_code,
            failure_reason=result.failure_reason,
            error=result.error,
            response_excerpt=result.response_excerpt,
            http_duration_ms=result.http_duration_ms,
        )

        if result.success:
            self.deliveries.mark_succeeded(delivery_id, attempts, result.status_code)
            return

        # 签名/时间戳被对端明确拒绝：配置不匹配，重试无意义，立即终态
        if result.failure_reason == "signature_rejected":
            self.deliveries.mark_terminal(
                delivery_id,
                status="failed_signature_rejected",
                attempts=attempts,
                reason="signature_rejected",
                status_code=result.status_code,
                error=result.response_excerpt or "signature/timestamp rejected",
            )
            return

        max_attempts = self.settings.max_attempts
        if attempts >= max_attempts:
            terminal_status = (
                "failed_subscriber_unreachable"
                if result.failure_reason in ("timeout", "connection_error")
                else "failed_retries_exhausted"
            )
            self.deliveries.mark_terminal(
                delivery_id,
                status=terminal_status,
                attempts=attempts,
                reason=result.failure_reason,
                status_code=result.status_code,
                error=result.error or result.response_excerpt,
            )
            return

        delay = backoff_delay(
            attempts,
            base=self.settings.base_delay,
            maximum=self.settings.max_delay,
        )
        next_after = (utcnow() + timedelta(seconds=delay)).isoformat()
        self.deliveries.mark_retry(
            delivery_id,
            attempts=attempts,
            reason=result.failure_reason,
            status_code=result.status_code,
            error=result.error or result.response_excerpt,
            next_attempt_after=next_after,
        )
