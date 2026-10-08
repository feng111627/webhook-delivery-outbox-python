"""端到端：真实 HTTP 投递到本机模拟接收端。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

SECRET = "receiver-local-secret-0123456789"


async def _receiver_events(base: str) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{base}/received")
        r.raise_for_status()
        return r.json()["events"]


@pytest.mark.asyncio
async def test_happy_path_and_duplicate_submit(components, receiver_base):
    service, worker, _ = components
    service.register_subscription(
        "ok-sub", f"{receiver_base}/hook/ok", SECRET, ["order.created"]
    )

    r1 = service.submit_event("evt-happy-1", "order.created", {"n": 1})
    assert r1["deduped"] is False and len(r1["new_deliveries"]) == 1

    # 完全相同的 event_id 再提交两次：不再产生新投递
    r2 = service.submit_event("evt-happy-1", "order.created", {"n": 1})
    r3 = service.submit_event("evt-happy-1", "order.created", {"n": 999})
    assert r2["deduped"] and r2["new_deliveries"] == []
    assert r3["deduped"] and r3["new_deliveries"] == []

    async def terminal():
        st = service.get_event_status("evt-happy-1")
        return st if all(d["terminal"] for d in st["deliveries"]) else None

    from tests.conftest import wait_for

    st = await wait_for(terminal)
    (d,) = st["deliveries"]
    assert d["status"] == "succeeded"
    assert d["attempts"] == 1

    events = await _receiver_events(receiver_base)
    matching = [e for e in events if e["event_id"] == "evt-happy-1"]
    assert len(matching) == 1  # 同一事件最多成功送达一次
    assert matching[0]["body"]["payload"] == {"n": 1}

@pytest.mark.asyncio
async def test_concurrent_duplicate_submits_create_single_delivery(
    components, receiver_base
):
    service, worker, _ = components
    service.register_subscription(
        "ok-sub", f"{receiver_base}/hook/ok", SECRET, ["order.created"]
    )

    # 20 个并发提交同一个 event_id
    results = await asyncio.gather(
        *[
            asyncio.get_running_loop().run_in_executor(
                None,
                service.submit_event,
                "evt-concurrent-1",
                "order.created",
                {"i": i},
            )
            for i in range(20)
        ]
    )
    first = [r for r in results if not r["deduped"]]
    assert len(first) == 1
    assert sum(len(r["new_deliveries"]) for r in results) == 1

    from tests.conftest import wait_for

    await wait_for(
        lambda: all(
            d["terminal"]
            for d in service.get_event_status("evt-concurrent-1")["deliveries"]
        )
    )
    events = await _receiver_events(receiver_base)
    assert len([e for e in events if e["event_id"] == "evt-concurrent-1"]) == 1
