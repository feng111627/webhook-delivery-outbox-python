"""Outbox 存储层：事件、订阅方、投递与投递尝试。

关键幂等/并发保证：
- events.event_id 唯一：重复提交事件靠唯一约束去重。
- deliveries(event_id, subscriber_id) 唯一：同一事件对同一订阅方只会有一条投递。
- claim_due_deliveries 用 BEGIN IMMEDIATE 事务原子认领，配合进程内 RLock，
  API 线程与多个 worker 线程共享同一连接也不会交错事务；多进程部署时
  SQLite 写锁本身也保证认领互斥。

本服务为本地单机设计，存储层用一把可重入锁串行化所有数据库访问；
SQLite 单库写入吞吐对 webhook 投递场景足够。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from app.models import (
    Attempt,
    AttemptOutcome,
    Delivery,
    DeliveryStatus,
    Event,
    FailureReason,
    Subscriber,
    SubscriberStatus,
)


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _row_to_subscriber(row: sqlite3.Row) -> Subscriber:
    return Subscriber(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        secret=row["secret"],
        event_types=json.loads(row["event_types"]),
        status=SubscriberStatus(row["status"]),
        created_at=row["created_at"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
    )


def _row_to_delivery(row: sqlite3.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        event_id=row["event_id"],
        subscriber_id=row["subscriber_id"],
        status=DeliveryStatus(row["status"]),
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        locked_until=row["locked_until"],
        failure_reason=FailureReason(row["failure_reason"]),
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        id=row["id"],
        delivery_id=row["delivery_id"],
        attempt_no=row["attempt_no"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=AttemptOutcome(row["outcome"]),
        http_status=row["http_status"],
        error_kind=row["error_kind"],
        receiver_code=row["receiver_code"],
        detail=row["detail"],
    )


class OutboxStorage:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    # ---- 订阅方 ----
    def create_subscriber(self, subscriber: Subscriber) -> Subscriber:
        with self._lock:
            self.conn.execute(
                "INSERT INTO subscribers (id, name, url, secret, event_types, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    subscriber.id,
                    subscriber.name,
                    subscriber.url,
                    subscriber.secret,
                    json.dumps(subscriber.event_types),
                    subscriber.status.value,
                    subscriber.created_at,
                ),
            )
            self.conn.commit()
        return subscriber

    def get_subscriber(self, subscriber_id: str) -> Subscriber | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)
            ).fetchone()
        return _row_to_subscriber(row) if row else None

    def list_subscribers(self, include_disabled: bool = True) -> list[Subscriber]:
        with self._lock:
            if include_disabled:
                rows = self.conn.execute(
                    "SELECT * FROM subscribers ORDER BY created_at"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM subscribers WHERE status = ? ORDER BY created_at",
                    (SubscriberStatus.ACTIVE.value,),
                ).fetchall()
        return [_row_to_subscriber(r) for r in rows]

    def list_active_subscribers_for_event(self, event_type: str) -> list[Subscriber]:
        rows = self.list_subscribers(include_disabled=False)
        return [s for s in rows if event_type in s.event_types]

    def set_subscriber_status(self, subscriber_id: str, active: bool) -> bool:
        status = SubscriberStatus.ACTIVE if active else SubscriberStatus.DISABLED
        with self._lock:
            cur = self.conn.execute(
                "UPDATE subscribers SET status = ? WHERE id = ?", (status.value, subscriber_id)
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ---- 事件 ----
    def insert_event(self, event_id: str, event_type: str, payload: dict) -> tuple[Event, bool]:
        """原子写入事件。已存在则返回旧记录，created=False。"""
        now = _now()
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO events (event_id, event_type, payload, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (event_id, event_type, json.dumps(payload, ensure_ascii=False), now),
                )
                self.conn.commit()
                created = True
            except sqlite3.IntegrityError:
                self.conn.rollback()
                created = False
            row = self.conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return _row_to_event(row), created

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return _row_to_event(row) if row else None

    # ---- 投递 ----
    def create_deliveries(self, event_id: str, subscriber_ids: list[str], now: float) -> list[Delivery]:
        """为事件 × 订阅方创建待投递；(event_id, subscriber_id) 冲突的跳过。"""
        deliveries: list[Delivery] = []
        with self._lock:
            for sub_id in subscriber_ids:
                did = _new_id("dlv")
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO deliveries "
                    "(id, event_id, subscriber_id, status, attempts, next_attempt_at, "
                    " locked_until, failure_reason, last_error, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, 0, NULL, 'none', NULL, ?, ?)",
                    (did, event_id, sub_id, DeliveryStatus.PENDING.value, now, now),
                )
                if cur.rowcount > 0:
                    row = self.conn.execute(
                        "SELECT * FROM deliveries WHERE id = ?", (did,)
                    ).fetchone()
                    deliveries.append(_row_to_delivery(row))
            self.conn.commit()
        return deliveries

    def find_active_delivery(self, event_id: str, subscriber_id: str) -> Delivery | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM deliveries WHERE event_id = ? AND subscriber_id = ?",
                (event_id, subscriber_id),
            ).fetchone()
        return _row_to_delivery(row) if row else None

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return _row_to_delivery(row) if row else None

    def list_deliveries(
        self, event_id: str | None = None, subscriber_id: str | None = None
    ) -> list[Delivery]:
        sql = "SELECT * FROM deliveries WHERE 1=1"
        params: list = []
        if event_id is not None:
            sql += " AND event_id = ?"
            params.append(event_id)
        if subscriber_id is not None:
            sql += " AND subscriber_id = ?"
            params.append(subscriber_id)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_delivery(r) for r in rows]

    def count_due(self, now: float) -> int:
        """统计当前到期可认领（含在途锁过期）的投递数，不做任何加锁。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM deliveries"
                " WHERE status = ? AND next_attempt_at <= ?"
                "   AND (locked_until IS NULL OR locked_until < ?)",
                (DeliveryStatus.PENDING.value, now, now),
            ).fetchone()
        return int(row[0])

    def claim_due_deliveries(self, now: float, limit: int, lock_seconds: float) -> list[Delivery]:
        """原子认领到期投递。

        BEGIN IMMEDIATE 立即取得 SQLite 写锁，写事务在全库串行化：
        第一个线程选中并加锁后提交，后续线程再 SELECT 时这些行已被锁定，
        因此多线程下同一条投递绝不会被并发送出。
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT id FROM deliveries"
                        " WHERE status = ? AND next_attempt_at <= ?"
                        "   AND (locked_until IS NULL OR locked_until < ?)"
                        " ORDER BY next_attempt_at LIMIT ?",
                        (DeliveryStatus.PENDING.value, now, now, limit),
                    ).fetchall()
                ]
                if ids:
                    deadline = now + lock_seconds
                    self.conn.executemany(
                        "UPDATE deliveries SET locked_until = ?, updated_at = ? WHERE id = ?",
                        [(deadline, now, i) for i in ids],
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return [d for d in (self.get_delivery(i) for i in ids) if d is not None]

    def record_attempt_and_schedule(
        self, delivery: Delivery, attempt: Attempt, now: float, next_at: float
    ) -> None:
        """记录一次失败尝试并按退避重新排队，同时释放锁。"""
        with self._lock:
            self._insert_attempt(attempt)
            self.conn.execute(
                "UPDATE deliveries SET attempts = ?, next_attempt_at = ?, locked_until = NULL,"
                " last_error = ?, updated_at = ? WHERE id = ?",
                (attempt.attempt_no, next_at, attempt.detail, now, delivery.id),
            )
            self.conn.commit()

    def mark_succeeded(self, delivery: Delivery, attempt: Attempt, now: float) -> None:
        with self._lock:
            self._insert_attempt(attempt)
            self.conn.execute(
                "UPDATE deliveries SET status = ?, attempts = ?, locked_until = NULL,"
                " failure_reason = 'none', last_error = NULL, updated_at = ? WHERE id = ?",
                (DeliveryStatus.SUCCEEDED.value, attempt.attempt_no, now, delivery.id),
            )
            self.conn.commit()

    def mark_failed(
        self, delivery: Delivery, attempt: Attempt, reason: FailureReason, now: float
    ) -> None:
        with self._lock:
            self._insert_attempt(attempt)
            self.conn.execute(
                "UPDATE deliveries SET status = ?, attempts = ?, locked_until = NULL,"
                " failure_reason = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (
                    DeliveryStatus.FAILED.value,
                    attempt.attempt_no,
                    reason.value,
                    attempt.detail,
                    now,
                    delivery.id,
                ),
            )
            self.conn.commit()

    def _insert_attempt(self, attempt: Attempt) -> None:
        self.conn.execute(
            "INSERT INTO delivery_attempts"
            " (delivery_id, attempt_no, started_at, finished_at, outcome,"
            "  http_status, error_kind, receiver_code, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt.delivery_id,
                attempt.attempt_no,
                attempt.started_at,
                attempt.finished_at,
                attempt.outcome.value,
                attempt.http_status,
                attempt.error_kind,
                attempt.receiver_code,
                attempt.detail,
            ),
        )

    def list_attempts(self, delivery_id: str) -> list[Attempt]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM delivery_attempts WHERE delivery_id = ? ORDER BY attempt_no",
                (delivery_id,),
            ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    def release_stale_locks(self, now: float) -> int:
        """崩溃/超时遗留的锁重新释放为可认领。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE deliveries SET locked_until = NULL"
                " WHERE status = ? AND locked_until IS NOT NULL AND locked_until < ?",
                (DeliveryStatus.PENDING.value, now),
            )
            self.conn.commit()
            return cur.rowcount
