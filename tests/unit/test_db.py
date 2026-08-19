"""SQLite 建库与并发安全 ID 生成测试（设计文档 §6 / §22.1）。"""

import sqlite3
from pathlib import Path

from common.ids import idempotency_key, temp_task_id
from state.db import MIGRATIONS_DIR, init_db, migrate, next_task_id


def test_init_db_creates_tables_and_wal(tmp_path):
    conn = init_db(tmp_path / "state.db")
    journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal == "wal"
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    }
    assert {
        "agents", "tasks", "artifacts", "task_runs", "events", "counters",
        "conversations", "collaborations", "conversation_messages",
        "agent_session_bindings", "action_intents",
        "agent_templates", "agent_profiles", "agent_profile_versions",
    } <= tables


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


def test_migrations_upgrade_existing_database(tmp_path):
    """Upgrade preserves pre-004 tasks and adds collaboration/profile data."""
    db = tmp_path / "pre-004.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    for version in (1, 2, 3):
        path = next(Path(MIGRATIONS_DIR).glob(f"{version:03d}_*.sql"))
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
            (version, "2026-08-19T00:00:00+08:00"))
    conn.execute(
        "INSERT INTO tasks (id, status, objective, created_at, updated_at)"
        " VALUES ('T-old', 'queued', 'keep me', 'now', 'now');")
    conn.commit()

    assert migrate(conn) == [4, 5, 6, 7]
    assert conn.execute(
        "SELECT objective FROM tasks WHERE id = 'T-old';").fetchone()[0] == "keep me"
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks);")}
    assert "collaboration_id" in columns
    agent_columns = {r[1] for r in conn.execute("PRAGMA table_info(agents);")}
    assert {"template_id", "profile_id"} <= agent_columns
    binding_columns = {
        r[1] for r in conn.execute(
            "PRAGMA table_info(agent_session_bindings);")}
    assert {
        "adapter_session_id", "capabilities_json", "recovery_state",
        "replacement_of_id", "last_error",
    } <= binding_columns
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name = 'agent_session_interactions';"
    ).fetchone() is not None
