"""SQLite 建库与并发安全 ID 生成测试（设计文档 §6 / §22.1）。"""

import sqlite3

from common.ids import idempotency_key, temp_task_id
from state.db import init_db, next_task_id


def test_init_db_creates_tables_and_wal(tmp_path):
    conn = init_db(tmp_path / "state.db")
    journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal == "wal"
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    }
    assert {"agents", "tasks", "artifacts", "task_runs", "events", "counters"} <= tables


def test_next_task_id_increments(tmp_path):
    conn = init_db(tmp_path / "state.db")
    first = next_task_id(conn)
    second = next_task_id(conn)
    assert first != second
    assert first.endswith("-0001")
    assert second.endswith("-0002")


def test_idempotency_key_unique_constraint(tmp_path):
    conn = init_db(tmp_path / "state.db")
    key = idempotency_key("T-20260817-0001", 1)
    row = (
        "T-20260817-0001",
        "queued",
        "test objective",
        key,
        "2026-08-17T12:00:00+08:00",
        "2026-08-17T12:00:00+08:00",
    )
    sql = (
        "INSERT INTO tasks (id, status, objective, idempotency_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?);"
    )
    conn.execute(sql, row)
    conn.commit()
    try:
        conn.execute(sql, ("T-20260817-0002", "queued", "dup", key, *row[4:]))
        conn.commit()
        raise AssertionError("duplicate idempotency_key should fail")
    except sqlite3.IntegrityError:
        pass


def test_temp_task_id_format():
    tid = temp_task_id()
    assert tid.startswith("T-")
    parts = tid.split("-")
    assert len(parts) == 4  # T, date, time, rand
