"""SQLite 持久化层（outbox 表结构与连接初始化）。

去重的关键约束：

* ``events.event_id`` 唯一 —— 重复提交同一业务事件只会落一条。
* ``deliveries(event_id, subscription_id)`` 唯一 —— 同一事件对同一
  订阅方最多只有一条投递记录，因此最多成功送达一次。

FastAPI 的同步路由运行在线程池中，可能与后台 worker 线程并发访问同一
连接。SQLite 连接对象本身不支持跨线程并发调用，因此这里用一把
可重入锁把所有 execute/commit 串行化；所有操作都是毫秒级本地读写，
真正耗时的 HTTP 投递发生在锁外，不影响投递并发性。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    event_types TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_after TEXT,
    last_failure_reason TEXT NOT NULL DEFAULT 'none',
    last_response_code INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(status, next_attempt_after);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    attempt_no INTEGER NOT NULL,
    status_code INTEGER,
    failure_reason TEXT NOT NULL,
    error TEXT,
    response_excerpt TEXT,
    http_duration_ms REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_delivery
    ON delivery_attempts(delivery_id, attempt_no);
"""


class LockingConnection:
    """对 sqlite3.Connection 的薄封装：所有写/读/事务调用串行化。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self.row_factory = conn.row_factory

    def execute(self, sql: str, params=()):
        """写语句使用；返回 lastrowid/rowcount 后即不再使用游标。"""
        with self._lock:
            return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def executemany(self, sql: str, params):
        with self._lock:
            return self._conn.executemany(sql, params)

    def executescript(self, script: str):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """进入整段临界区；区内的 execute/commit 是同把可重入锁。"""
        with self._lock:
            yield


def connect(database_path: str) -> LockingConnection:
    """打开连接。``:memory:`` 用于单测；文件路径会自动创建父目录。"""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return LockingConnection(conn)


def init_db(conn: LockingConnection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA)
    conn.commit()
