"""数据库连接与迁移 — 设计文档 §6 / §17.8；Evolution v3 §4 双后端。

后端由 LAS_DATABASE_URL 决定（common/config.database_url）：
  postgresql://user:pass@host:5432/db   PostgreSQL（compose 默认/外部）
  sqlite:////abs/path.db                SQLite（轻量/单机回退）
connect() 也接受裸路径（向后兼容，按 SQLite 处理）。

方言差异集中在本模块与 migrations_pg/：
  - 占位符：调用点统一写 `?`，PG 连接包装器翻译为 `%s`
  - 行访问：PgRow 同时支持 int/str 下标（对齐 sqlite3.Row 行为）
  - counters upsert / approval_grants 自增 id：见 next_task_id 与迁移

迁移机制：src/state/migrations/NNN_name.sql（SQLite）或
src/state/migrations_pg/NNN_name.sql（PostgreSQL）按版本号顺序应用，
已应用版本记录在 schema_migrations 表。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATIONS_PG_DIR = Path(__file__).parent / "migrations_pg"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---------- PostgreSQL 适配 ----------


class PgRow:
    """同时支持 int / str 下标的行（对齐 sqlite3.Row 行为）。"""

    __slots__ = ("_cols", "_vals", "_idx")

    def __init__(self, cols: list[str], vals: tuple):
        self._cols = cols
        self._vals = vals
        self._idx = {c: i for i, c in enumerate(cols)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._idx[key]]

    def keys(self) -> list[str]:
        return list(self._cols)


class PgCursor:
    """包装 psycopg cursor：fetch* 返回 PgRow，透传 rowcount。"""

    def __init__(self, cur):
        self._cur = cur

    def _wrap(self, row):
        if row is None:
            return None
        cols = [d.name for d in self._cur.description]
        return PgRow(cols, row)

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):  # 对齐 sqlite3.Cursor 可直接迭代的行为
        for row in self._cur:
            yield self._wrap(row)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class PgConnection:
    """psycopg 连接的薄包装：`?` → `%s`，execute 返回 PgCursor。"""

    backend = "pg"

    def __init__(self, url: str):
        import psycopg

        self._conn = psycopg.connect(url, autocommit=False)

    def execute(self, sql: str, params=()) -> PgCursor:
        return PgCursor(self._conn.execute(sql.replace("?", "%s"), params))

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


# ---------- 连接入口 ----------


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    """打开数据库并启用 WAL（§17.8）；父目录不存在时自动创建。

    check_same_thread=False：ASGI 服务（webui / orchestrator-a2a）的连接
    在请求处理线程使用，与建连线程不同；并发写安全由 busy_timeout +
    单写者纪律（§22.3）保障。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # 双写方（StateWriter + TaskManager）并发时等待而非立刻报错
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def connect(target=None):
    """打开数据库。target：None → LAS_DATABASE_URL；Path/裸路径 → SQLite；
    含 :// 的 str → 按 scheme 分发（sqlite:///、postgresql://）。"""
    from common import config as cfg

    if target is None:
        target = cfg.database_url()
    if isinstance(target, Path):
        return _connect_sqlite(target)
    target = str(target)
    if target.startswith("sqlite:///"):
        return _connect_sqlite(Path(target[len("sqlite:///"):]))
    if target.startswith(("postgresql://", "postgres://")):
        return PgConnection(target)
    if "://" not in target:  # 裸路径，向后兼容
        return _connect_sqlite(Path(target))
    raise ValueError(f"unsupported database url: {target.split(':', 1)[0]}://…")


def _backend(conn) -> str:
    return getattr(conn, "backend", "sqlite")


def _migration_versions(conn) -> tuple[Path, set[int], list[int]]:
    migrations_dir = (MIGRATIONS_PG_DIR if _backend(conn) == "pg"
                      else MIGRATIONS_DIR)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
    )
    conn.commit()
    applied = {
        r[0] for r in conn.execute("SELECT version FROM schema_migrations;")
    }
    available = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = re.match(r"^(\d+)_", path.name)
        if match:
            available.append(int(match.group(1)))
    return migrations_dir, applied, [v for v in available if v not in applied]


def _validate_migration_backup(pending: list[int], applied: set[int]) -> Path | None:
    from common import config as cfg

    # A pristine database has nothing to protect and cannot be backed up through
    # the running control plane yet. The gate applies only to upgrades.
    if not pending or not applied or not cfg.require_migration_backup():
        return None
    receipt_path = cfg.migration_backup_receipt()
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(receipt["created_at"])
        if created.tzinfo is None:
            raise ValueError
        age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)
               ).total_seconds()
        digest = receipt["archive_sha256"]
        if (receipt.get("format_version") != 1
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or age < -300 or age > cfg.migration_backup_max_age()):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"待执行数据库迁移 {pending}，但缺少有效且新鲜的一次性备份回执；"
            "先运行 control-plane-backup.py create") from exc
    return receipt_path


def migrate(conn) -> list[int]:
    """应用未执行的迁移，返回本次应用的版本列表。"""
    pg = _backend(conn) == "pg"
    migrations_dir, applied, pending = _migration_versions(conn)
    receipt_path = _validate_migration_backup(pending, applied) if pg else None
    consuming_path = None
    if receipt_path is not None:
        consuming_path = receipt_path.with_suffix(receipt_path.suffix + ".consuming")
        if consuming_path.exists():
            raise RuntimeError(
                "发现未完成迁移的 consuming 回执；必须先创建新的安全备份")
        receipt_path.replace(consuming_path)
    newly: list[int] = []
    try:
        for path in sorted(migrations_dir.glob("*.sql")):
            m = re.match(r"^(\d+)_", path.name)
            if not m:
                continue
            version = int(m.group(1))
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            if pg:
                # psycopg 单语句执行：按分号切（迁移 DDL 内无过程体）
                for stmt in sql.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
            else:
                conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                (version, now_iso()),
            )
            conn.commit()
            newly.append(version)
    except Exception:
        if consuming_path is not None and consuming_path.exists():
            consuming_path.replace(receipt_path)
        raise
    if consuming_path is not None:
        consuming_path.unlink()
    return newly


def init_db(target=None):
    conn = connect(target)
    migrate(conn)
    return conn


def next_task_id(conn, now: datetime | None = None) -> str:
    """并发安全 ID 生成（§22.1）：counters 表单事务 +1。"""
    from common.ids import counter_name, format_task_id

    name = counter_name(now)
    if _backend(conn) == "pg":
        sql = (
            "INSERT INTO counters (name, value) VALUES (%s, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = counters.value + 1 "
            "RETURNING value;"
        )
        cur = conn.execute(sql, (name,))
    else:
        cur = conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1 "
            "RETURNING value;",
            (name,),
        )
    seq = cur.fetchone()[0]
    conn.commit()
    return format_task_id(seq, now)
