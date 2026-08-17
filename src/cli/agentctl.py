"""agentctl — 观察与管理 CLI（设计文档 §Phase 8）。

当前命令：
  agentctl status                 环境体检（db / WAL / 表）
  agentctl agent list             Agent 列表
  agentctl task list [--status]   任务列表
  agentctl task show <id>         任务详情（含 runs / artifacts）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DB = Path.home() / "AgentWorkspace" / "runtime" / "agent-state.db"


def _conn(db_path: Path):
    from state.db import connect

    if not db_path.exists():
        print(f"state db not found: {db_path}")
        sys.exit(1)
    return connect(db_path)


def cmd_status(db_path: Path) -> int:
    if not db_path.exists():
        print(f"state db not found: {db_path}")
        return 1
    conn = _conn(db_path)
    journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")]
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version;")]
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
        for t in ("agents", "tasks", "artifacts", "task_runs", "events")
        if t in tables
    }
    print(f"db: {db_path}")
    print(f"journal_mode: {journal}")
    print(f"migrations: {versions}")
    print(f"tables: {', '.join(tables)}")
    print(f"counts: {counts}")
    return 0


def cmd_agent_list(db_path: Path) -> int:
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT id, role, status, endpoint, last_seen_at, lease_expires_at"
        " FROM agents ORDER BY id;").fetchall()
    if not rows:
        print("(no agents)")
        return 0
    print(f"{'ID':<12} {'ROLE':<14} {'STATUS':<10} {'LAST SEEN':<26} LEASE EXPIRES")
    for r in rows:
        print(f"{r['id']:<12} {r['role']:<14} {r['status']:<10}"
              f" {str(r['last_seen_at']):<26} {r['lease_expires_at']}")
    return 0


def cmd_task_list(db_path: Path, status: str | None) -> int:
    from orchestrator.state_store import list_tasks

    conn = _conn(db_path)
    rows = list_tasks(conn, status=status)
    if not rows:
        print("(no tasks)")
        return 0
    print(f"{'ID':<20} {'STATUS':<14} {'ASSIGNED':<10} {'RETRY':<6} OBJECTIVE")
    for r in rows:
        print(f"{r['id']:<20} {r['status']:<14} {str(r['assigned_to']):<10}"
              f" {r['retry_count']:<6} {r['objective'][:60]}")
    return 0


def cmd_task_show(db_path: Path, task_id: str) -> int:
    conn = _conn(db_path)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if row is None:
        print(f"task not found: {task_id}")
        return 1
    for key in row.keys():
        print(f"{key}: {row[key]}")
    runs = conn.execute(
        "SELECT agent_id, attempt, status, started_at, error_message"
        " FROM task_runs WHERE task_id = ? ORDER BY started_at;",
        (task_id,)).fetchall()
    print("\n-- runs --")
    for r in runs:
        print(f"  attempt={r['attempt']} agent={r['agent_id']}"
              f" status={r['status']} err={r['error_message']}")
    arts = conn.execute(
        "SELECT name, type, sha256, path FROM artifacts WHERE task_id = ?;",
        (task_id,)).fetchall()
    print("-- artifacts --")
    for a in arts:
        print(f"  {a['name']} ({a['type']}) sha256={a['sha256'][:12]}… {a['path']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")

    p_agent = sub.add_parser("agent")
    p_agent.add_subparsers(dest="sub", required=True).add_parser("list")

    p_task = sub.add_parser("task")
    task_sub = p_task.add_subparsers(dest="sub", required=True)
    p_tl = task_sub.add_parser("list")
    p_tl.add_argument("--status", default=None)
    p_ts = task_sub.add_parser("show")
    p_ts.add_argument("task_id")

    args = parser.parse_args()
    if args.command == "status":
        return cmd_status(args.db)
    if args.command == "agent" and args.sub == "list":
        return cmd_agent_list(args.db)
    if args.command == "task" and args.sub == "list":
        return cmd_task_list(args.db, args.status)
    if args.command == "task" and args.sub == "show":
        return cmd_task_show(args.db, args.task_id)
    return 2


if __name__ == "__main__":
    sys.exit(main())
