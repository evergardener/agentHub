"""SQLite 连接与迁移 — 设计文档 §6 / §17.8。

迁移机制：src/state/migrations/NNN_name.sql 按版本号顺序应用，
已应用版本记录在 schema_migrations 表。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数据库并启用 WAL（§17.8）；父目录不存在时自动创建。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # 双写方（StateWriter + TaskManager）并发时等待而非立刻报错
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def migrate(conn: sqlite3.Connection) -> list[int]:
    """应用未执行的迁移，返回本次应用的版本列表。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
    )
    applied = {
        r[0] for r in conn.execute("SELECT version FROM schema_migrations;")
    }
    newly: list[int] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"^(\d+)_", path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in applied:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
            (version, now_iso()),
        )
        conn.commit()
        newly.append(version)
    return newly


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


def next_task_id(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """并发安全 ID 生成（§22.1）：counters 表单事务 +1。"""
    from common.ids import counter_name, format_task_id

    name = counter_name(now)
    cur = conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1 "
        "RETURNING value;",
        (name,),
    )
    seq = cur.fetchone()[0]
    conn.commit()
    return format_task_id(seq, now)
