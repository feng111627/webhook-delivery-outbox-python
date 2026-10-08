import sqlite3

import pytest

from webhook.db import connect, init_db


def test_init_db_idempotent(tmp_path):
    db = tmp_path / "w.db"
    conn = connect(str(db))
    init_db(conn)
    init_db(conn)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"subscriptions", "events", "deliveries", "delivery_attempts"} <= tables
    conn.close()


def test_unique_constraints(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    init_db(conn)
    now = "2026-10-08T00:00:00+00:00"
    conn.execute(
        "INSERT INTO events(event_id,event_type,payload,created_at) VALUES(?,?,?,?)",
        ("e1", "order.created", "{}", now),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events(event_id,event_type,payload,created_at) VALUES(?,?,?,?)",
            ("e1", "order.created", "{}", now),
        )
