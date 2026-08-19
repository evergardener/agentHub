"""状态库、并发 ID 与生产迁移备份门禁测试。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


class _FakePg:
    backend = "pg"

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


def _existing_pg() -> _FakePg:
    conn = _FakePg()
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);")
    conn.execute(
        "INSERT INTO schema_migrations VALUES (1, '2026-08-19T00:00:00Z');")
    conn.commit()
    return conn


def _write_receipt(path: Path, created: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "format_version": 1,
        "created_at": created.isoformat(),
        "archive_sha256": "a" * 64,
    }), encoding="utf-8")


def test_pg_migration_requires_and_consumes_fresh_backup_receipt(
        tmp_path, monkeypatch):
    from state import db as db_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_existing.sql").write_text(
        "CREATE TABLE already_applied (id INTEGER);", encoding="utf-8")
    (migrations / "002_test.sql").write_text(
        "CREATE TABLE guarded (id INTEGER PRIMARY KEY);", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(db_module, "MIGRATIONS_PG_DIR", migrations)
    monkeypatch.setenv("LAS_REQUIRE_MIGRATION_BACKUP", "true")
    monkeypatch.setenv("LAS_MIGRATION_BACKUP_RECEIPT", str(receipt))

    conn = _existing_pg()
    with pytest.raises(RuntimeError, match="一次性备份回执"):
        db_module.migrate(conn)
    _write_receipt(receipt, datetime.now(timezone.utc))
    assert db_module.migrate(conn) == [2]
    assert not receipt.exists()
    assert not receipt.with_suffix(".json.consuming").exists()
    assert db_module.migrate(conn) == []


def test_pg_migration_rejects_stale_backup_receipt(tmp_path, monkeypatch):
    from state import db as db_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_existing.sql").write_text(
        "CREATE TABLE already_applied (id INTEGER);", encoding="utf-8")
    (migrations / "002_test.sql").write_text(
        "CREATE TABLE guarded (id INTEGER PRIMARY KEY);", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    _write_receipt(receipt, datetime.now(timezone.utc) - timedelta(days=2))
    monkeypatch.setattr(db_module, "MIGRATIONS_PG_DIR", migrations)
    monkeypatch.setenv("LAS_REQUIRE_MIGRATION_BACKUP", "true")
    monkeypatch.setenv("LAS_MIGRATION_BACKUP_RECEIPT", str(receipt))
    with pytest.raises(RuntimeError, match="一次性备份回执"):
        db_module.migrate(_existing_pg())


def test_pg_migration_error_restores_receipt_for_retry(tmp_path, monkeypatch):
    from state import db as db_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_existing.sql").write_text(
        "CREATE TABLE already_applied (id INTEGER);", encoding="utf-8")
    (migrations / "002_bad.sql").write_text(
        "THIS IS NOT SQL;", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    _write_receipt(receipt, datetime.now(timezone.utc))
    monkeypatch.setattr(db_module, "MIGRATIONS_PG_DIR", migrations)
    monkeypatch.setenv("LAS_REQUIRE_MIGRATION_BACKUP", "true")
    monkeypatch.setenv("LAS_MIGRATION_BACKUP_RECEIPT", str(receipt))
    with pytest.raises(sqlite3.OperationalError):
        db_module.migrate(_existing_pg())
    assert receipt.is_file()
    assert not receipt.with_suffix(".json.consuming").exists()


def test_pg_pristine_bootstrap_does_not_require_backup(tmp_path, monkeypatch):
    from state import db as db_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_bootstrap.sql").write_text(
        "CREATE TABLE initial (id INTEGER PRIMARY KEY);", encoding="utf-8")
    monkeypatch.setattr(db_module, "MIGRATIONS_PG_DIR", migrations)
    monkeypatch.setenv("LAS_REQUIRE_MIGRATION_BACKUP", "true")
    monkeypatch.setenv("LAS_MIGRATION_BACKUP_RECEIPT",
                       str(tmp_path / "missing.json"))
    assert db_module.migrate(_FakePg()) == [1]
