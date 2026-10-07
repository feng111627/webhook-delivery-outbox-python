"""SQLite 连接与表结构初始化。"""
from __future__ import annotations

import os
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscribers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL,
    secret       TEXT NOT NULL,
    event_types  TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    subscriber_id   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    locked_until    REAL,
    failure_reason  TEXT NOT NULL DEFAULT 'none',
    last_error      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(event_id, subscriber_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id   TEXT NOT NULL,
    attempt_no    INTEGER NOT NULL,
    started_at    REAL NOT NULL,
    finished_at   REAL NOT NULL,
    outcome       TEXT NOT NULL,
    http_status   INTEGER,
    error_kind    TEXT,
    receiver_code TEXT,
    detail        TEXT,
    UNIQUE(delivery_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_attempts_delivery
    ON delivery_attempts(delivery_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 让后台 worker 与 API 写入互不阻塞（:memory: 不支持 WAL）
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
