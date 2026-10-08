"""并发 claim 必须保证同一条投递只被一个 worker 抢到。"""
from __future__ import annotations

import asyncio

from webhook.config import Settings
from webhook.db import connect, init_db
from webhook.repositories import (
    DeliveryRepository,
    EventRepository,
    SubscriptionRepository,
)
from webhook.service import WebhookService
from webhook.worker import DeliveryWorker


class LatchDeliverer:
    """所有投递在同一道屏障上汇合，制造最大并发后再放行。"""

    def __init__(self, n: int) -> None:
        self.barrier = asyncio.Barrier(n)
        self.delivered: list[str] = []

    async def deliver(self, url, secret, event_id, event_type, payload, timestamp=None):
        async with self.barrier:
            self.delivered.append(event_id)
        from webhook.delivery import DeliveryResult

        return DeliveryResult(True, 200, "none", None, "ok")


async def test_two_workers_do_not_double_deliver(tmp_path):
    db = str(tmp_path / "c.db")
    conn = connect(db)
    init_db(conn)
    subs, events, dels = (
        SubscriptionRepository(conn),
        EventRepository(conn),
        DeliveryRepository(conn),
    )
    svc = WebhookService(subs, events, dels)
    svc.register_subscription("s", "http://x/hook", "0123456789abcdef", ["t"])

    settings = Settings(database_path=db, poll_interval=1000, max_attempts=3,
                        base_delay=0.01, max_delay=0.05)
    for i in range(5):
        svc.submit_event(f"evt-{i}", "t", {})

    # 两个独立 worker（不同连接、不同 deliverer）同时跑一轮
    conn2 = connect(db)
    s2, e2, d2 = SubscriptionRepository(conn2), EventRepository(conn2), DeliveryRepository(conn2)
    deliverer1, deliverer2 = LatchDeliverer(5), LatchDeliverer(5)
    w1 = DeliveryWorker(subs, events, dels, deliverer1, settings)
    w2 = DeliveryWorker(s2, e2, d2, deliverer2, settings)

    await asyncio.gather(w1.run_once(), w2.run_once())

    await asyncio.sleep(0.05)
    all_delivered = deliverer1.delivered + deliverer2.delivered
    assert sorted(all_delivered) == [f"evt-{i}" for i in range(5)]
    for i in range(5):
        st = svc.get_event_status(f"evt-{i}")
        assert st["deliveries"][0]["status"] == "succeeded"
        assert st["deliveries"][0]["attempts"] == 1
    conn2.close()
    conn.close()
