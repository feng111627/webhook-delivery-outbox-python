"""数据访问：订阅方 / 事件 / 投递记录 / 每次尝试明细。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from webhook.models import (
    Delivery,
    DeliveryStatus,
    Event,
    FailureReason,
    Subscription,
    SubscriptionStatus,
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _row_to_subscription(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        secret=row["secret"],
        event_types=json.loads(row["event_types"]),
        status=SubscriptionStatus(row["status"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        payload=row["payload"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        event_id=row["event_id"],
        subscription_id=row["subscription_id"],
        status=DeliveryStatus(row["status"]),
        attempts=row["attempts"],
        next_attempt_after=_parse_dt(row["next_attempt_after"])
        if row["next_attempt_after"]
        else None,
        last_failure_reason=FailureReason(row["last_failure_reason"]),
        last_response_code=row["last_response_code"],
        last_error=row["last_error"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SubscriptionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self, name: str, url: str, secret: str, event_types: list[str]
    ) -> Subscription:
        now = utcnow_iso()
        cur = self.conn.execute(
            """
            INSERT INTO subscriptions(name,url,secret,event_types,status,created_at,updated_at)
            VALUES(?,?,?,?,'active',?,?)
            """,
            (name, url, secret, json.dumps(event_types), now, now),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    def get(self, subscription_id: int) -> Subscription | None:
        row = self.conn.fetchone(
            "SELECT * FROM subscriptions WHERE id=?", (subscription_id,)
        )
        return _row_to_subscription(row) if row else None

    def get_by_name(self, name: str) -> Subscription | None:
        row = self.conn.fetchone(
            "SELECT * FROM subscriptions WHERE name=?", (name,)
        )
        return _row_to_subscription(row) if row else None

    def list_all(self) -> list[Subscription]:
        rows = self.conn.fetchall("SELECT * FROM subscriptions ORDER BY id")
        return [_row_to_subscription(r) for r in rows]

    def list_active_for_event(self, event_type: str) -> list[Subscription]:
        """活跃且关心该事件类型的订阅方。"""
        rows = self.conn.fetchall(
            "SELECT * FROM subscriptions WHERE status='active' ORDER BY id"
        )
        return [
            _row_to_subscription(r)
            for r in rows
            if event_type in json.loads(r["event_types"])
        ]

    def set_status(self, subscription_id: int, active: bool) -> Subscription:
        status = SubscriptionStatus.ACTIVE if active else SubscriptionStatus.DISABLED
        self.conn.execute(
            "UPDATE subscriptions SET status=?, updated_at=? WHERE id=?",
            (status.value, utcnow_iso(), subscription_id),
        )
        self.conn.commit()
        sub = self.get(subscription_id)
        if sub is None:  # pragma: no cover - 调用方保证存在
            raise ValueError(f"subscription {subscription_id} vanished")
        return sub


class EventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_if_absent(
        self, event_id: str, event_type: str, payload: str
    ) -> tuple[Event, bool]:
        """返回 (事件, 是否首次创建)。重复 event_id 返回已有记录。"""
        now = utcnow_iso()
        with self.conn.transaction():
            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO events(event_id,event_type,payload,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (event_id, event_type, payload, now),
                )
                self.conn.commit()
                return self.get_by_pk(cur.lastrowid), True  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                self.conn.rollback()
                existing = self.get(event_id)
                assert existing is not None
                return existing, False

    def get(self, event_id: str) -> Event | None:
        row = self.conn.fetchone(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        )
        return _row_to_event(row) if row else None

    def get_by_pk(self, pk: int) -> Event | None:
        row = self.conn.fetchone("SELECT * FROM events WHERE id=?", (pk,))
        return _row_to_event(row) if row else None


class DeliveryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_if_absent(
        self, event_pk: int, subscription_id: int
    ) -> tuple[Delivery | None, bool]:
        """幂等创建投递记录。已存在时返回 (None, False)。"""
        now = utcnow_iso()
        with self.conn.transaction():
            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO deliveries(event_id,subscription_id,status,attempts,
                                           last_failure_reason,created_at,updated_at)
                    VALUES(?,?,'pending',0,'none',?,?)
                    """,
                    (event_pk, subscription_id, now, now),
                )
                self.conn.commit()
                row = self.conn.execute(
                    "SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                return _row_to_delivery(row), True
            except sqlite3.IntegrityError:
                self.conn.rollback()
                return None, False

    def claim_due(self, now_iso: str, limit: int = 100) -> list[Delivery]:
        """取出到期可处理的投递：pending 立即可发，retrying 到点可发。

        取数后在同一临界区里用条件 UPDATE 抢占，保证并发 worker 不会
        抢到同一条（状态只能从 pending/retrying 变成 in_flight）。
        """
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT * FROM deliveries
                WHERE status='pending'
                   OR (status='retrying' AND next_attempt_after <= ?)
                ORDER BY id
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
            claimed: list[Delivery] = []
            for row in rows:
                cur = self.conn.execute(
                    """
                    UPDATE deliveries SET status='in_flight', updated_at=?
                    WHERE id=? AND status IN ('pending','retrying')
                    """,
                    (now_iso, row["id"]),
                )
                if cur.rowcount == 1:
                    claimed.append(_row_to_delivery(row))
            self.conn.commit()
            return claimed

    def mark_succeeded(
        self, delivery_id: int, attempts: int, status_code: int
    ) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET status='succeeded', attempts=?, last_failure_reason='none',
                last_response_code=?, last_error=NULL,
                next_attempt_after=NULL, updated_at=?
            WHERE id=?
            """,
            (attempts, status_code, utcnow_iso(), delivery_id),
        )
        self.conn.commit()

    def mark_retry(
        self,
        delivery_id: int,
        attempts: int,
        reason: str,
        status_code: int | None,
        error: str | None,
        next_attempt_after: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET status='retrying', attempts=?, last_failure_reason=?,
                last_response_code=?, last_error=?, next_attempt_after=?,
                updated_at=?
            WHERE id=?
            """,
            (
                attempts,
                reason,
                status_code,
                error,
                next_attempt_after,
                utcnow_iso(),
                delivery_id,
            ),
        )
        self.conn.commit()

    def mark_terminal(
        self,
        delivery_id: int,
        status: str,
        attempts: int,
        reason: str,
        status_code: int | None,
        error: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE deliveries
            SET status=?, attempts=?, last_failure_reason=?,
                last_response_code=?, last_error=?, next_attempt_after=NULL,
                updated_at=?
            WHERE id=?
            """,
            (
                status,
                attempts,
                reason,
                status_code,
                error,
                utcnow_iso(),
                delivery_id,
            ),
        )
        self.conn.commit()

    def requeue_in_flight(self) -> int:
        """关闭/崩溃残留的 in_flight 记录退回 pending（启动时调用）。"""
        cur = self.conn.execute(
            """
            UPDATE deliveries SET status='pending', updated_at=?
            WHERE status='in_flight'
            """,
            (utcnow_iso(),),
        )
        self.conn.commit()
        return cur.rowcount

    def skip_pending_for_subscription(self, subscription_id: int) -> int:
        """停用时把尚未成功的待发/重试中投递标记为跳过终态。"""
        cur = self.conn.execute(
            """
            UPDATE deliveries
            SET status='skipped_subscriber_disabled',
                last_failure_reason='unreachable',
                updated_at=?
            WHERE subscription_id=?
              AND status IN ('pending','retrying','in_flight')
            """,
            (utcnow_iso(), subscription_id),
        )
        self.conn.commit()
        return cur.rowcount

    def get(self, delivery_id: int) -> Delivery | None:
        row = self.conn.fetchone(
            "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
        )
        return _row_to_delivery(row) if row else None

    def list_for_event(self, event_pk: int) -> list[Delivery]:
        rows = self.conn.fetchall(
            "SELECT * FROM deliveries WHERE event_id=? ORDER BY id", (event_pk,)
        )
        return [_row_to_delivery(r) for r in rows]

    def add_attempt(
        self,
        delivery_id: int,
        attempt_no: int,
        status_code: int | None,
        failure_reason: str,
        error: str | None,
        response_excerpt: str | None,
        http_duration_ms: float,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO delivery_attempts(delivery_id,attempt_no,status_code,
                                          failure_reason,error,response_excerpt,
                                          http_duration_ms,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                attempt_no,
                status_code,
                failure_reason,
                error,
                response_excerpt,
                http_duration_ms,
                utcnow_iso(),
            ),
        )
        self.conn.commit()

    def list_attempts(self, delivery_id: int) -> list[dict]:
        rows = self.conn.fetchall(
            "SELECT * FROM delivery_attempts WHERE delivery_id=? ORDER BY attempt_no",
            (delivery_id,),
        )
        return [dict(r) for r in rows]
