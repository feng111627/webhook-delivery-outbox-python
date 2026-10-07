"""后台投递工作线程池。

设计要点：
- 业务提交事件后立即返回，投递由本 worker 轮询 outbox 后在线程池中异步完成。
- 每条 due 投递提交给独立工作线程：某个订阅方响应很慢，只会占住它自己那次任务，
  不会拖住其它订阅方或后续事件（线程池大小可配）。
- 认领由存储层原子完成，多个 worker 线程不会重复发送同一条投递。
- 退避/重试由 delivery 的 next_attempt_at 表达，worker 只负责轮询到期任务。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings
from app.deliveryexecutor import DeliveryExecutor
from app.storage import OutboxStorage

logger = logging.getLogger("webhook.worker")


class DeliveryWorker:
    def __init__(
        self,
        storage: OutboxStorage,
        executor: DeliveryExecutor,
        settings: Settings,
    ) -> None:
        self.storage = storage
        self.executor = executor
        self.settings = settings
        self._stop = threading.Event()
        self._pool: ThreadPoolExecutor | None = None
        self._thread: threading.Thread | None = None
        self._inflight: set = set()
        self._inflight_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._pool = ThreadPoolExecutor(
            max_workers=self.settings.worker_threads, thread_name_prefix="webhook-dlv"
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="webhook-worker", daemon=True)
        self._thread.start()
        logger.info("投递 worker 已启动 threads=%s", self.settings.worker_threads)

    def stop(self, timeout: float | None = None) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        logger.info("投递 worker 已停止")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """测试/演示辅助：等待所有到期投递被处理完（无 due 且无在途任务）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._inflight_lock:
                busy = bool(self._inflight)
            if not busy and not self._has_due():
                return True
            time.sleep(0.02)
        return False

    def inflight_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    def _has_due(self) -> bool:
        return self.storage.count_due(time.time()) > 0

    def _run_loop(self) -> None:
        pool = self._pool
        assert pool is not None
        while not self._stop.is_set():
            try:
                # 先回收崩溃遗留锁，再认领到期投递
                self.storage.release_stale_locks(time.time())
                claimed = self.storage.claim_due_deliveries(
                    time.time(),
                    limit=self.settings.worker_threads * 2,
                    lock_seconds=self.settings.lock_seconds,
                )
                for delivery in claimed:
                    with self._inflight_lock:
                        self._inflight.add(delivery.id)
                    pool.submit(self._run_one, delivery.id)
            except Exception:
                logger.exception("worker 轮询出错")
            self._stop.wait(self.settings.poll_interval_seconds)

    def _run_one(self, delivery_id: str) -> None:
        try:
            delivery = self.storage.get_delivery(delivery_id)
            if delivery is not None:
                self.executor.run_delivery(delivery)
        except Exception:
            logger.exception("投递执行异常 delivery=%s", delivery_id)
        finally:
            with self._inflight_lock:
                self._inflight.discard(delivery_id)
