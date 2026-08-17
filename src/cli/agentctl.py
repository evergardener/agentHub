"""agentctl — 观察与管理 CLI（设计文档 §Phase 8）。

当前命令：
  agentctl status                 环境体检（db / WAL / 表）
  agentctl agent list             Agent 列表
  agentctl task list [--status]   任务列表
  agentctl task show <id>         任务详情（含 runs / artifacts）
  agentctl task retry <id>        失败任务重试（failed → queued）
  agentctl task cancel <id>       取消任务（级联取消后代）
  agentctl task approve <id>      审批通过（blocked → working）
  agentctl task reject <id>       审批拒绝（blocked → cancelled，级联）
  agentctl events [--follow]      事件流（SQLite 单一事实源，--follow 轮询追加）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

def _default_db() -> Path:
    from common import config as cfg

    return cfg.state_db()


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


def cmd_task_retry(db_path: Path, task_id: str) -> int:
    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store

    tm = TaskManager(db_path=db_path)
    try:
        tm.retry_task(task_id)
    except (KeyError, state_store.IllegalTransition, RuntimeError) as e:
        print(f"retry failed: {e}")
        return 1
    print(f"{task_id}: failed → queued")
    return 0


def cmd_task_cancel(db_path: Path, task_id: str) -> int:
    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store

    tm = TaskManager(db_path=db_path)
    try:
        n = tm.cancel_task(task_id)
    except (KeyError, state_store.IllegalTransition) as e:
        print(f"cancel failed: {e}")
        return 1
    if n == 0:
        print(f"{task_id}: nothing to cancel (already terminal)")
        return 1
    print(f"{task_id}: cancelled ({n} task(s) including descendants)")
    return 0


def cmd_task_approve(db_path: Path, task_id: str, notes: str) -> int:
    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store

    tm = TaskManager(db_path=db_path)
    try:
        tm.approve_task(task_id, notes=notes)
    except (KeyError, state_store.IllegalTransition) as e:
        print(f"approve failed: {e}")
        return 1
    print(f"{task_id}: blocked → working (approved by user)")
    return 0


def cmd_task_reject(db_path: Path, task_id: str, notes: str) -> int:
    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store

    tm = TaskManager(db_path=db_path)
    try:
        tm.reject_task(task_id, notes=notes)
    except (KeyError, state_store.IllegalTransition) as e:
        print(f"reject failed: {e}")
        return 1
    print(f"{task_id}: blocked → cancelled (rejected by user)")
    return 0


def _print_event(row) -> None:
    payload = row["payload_json"] or ""
    if len(payload) > 200:
        payload = payload[:200] + "…"
    print(f"{row['created_at']}  {row['event_type']:<24}"
          f" task={row['task_id'] or '-':<22} agent={row['agent_id'] or '-':<10}"
          f" {payload}")


def cmd_events(db_path: Path, follow: bool, interval: float,
               event_type: str | None) -> int:
    conn = _conn(db_path)

    def fetch(after: int):
        if event_type:
            return conn.execute(
                "SELECT rowid, * FROM events"
                " WHERE event_type = ? AND rowid > ? ORDER BY rowid;",
                (event_type, after)).fetchall()
        return conn.execute(
            "SELECT rowid, * FROM events WHERE rowid > ? ORDER BY rowid;",
            (after,)).fetchall()

    last = 0
    while True:
        rows = fetch(last)
        for r in rows:
            _print_event(r)
            last = r["rowid"]
        if not follow:
            return 0
        time.sleep(interval)


def cmd_grant_list(db_path: Path, show_all: bool) -> int:
    from hermes.policy import ApprovalPolicy

    conn = _conn(db_path)
    rows = ApprovalPolicy.list_grants(conn, active_only=not show_all)
    if not rows:
        print("(no grants)")
        return 0
    print(f"{'ID':<5} {'PATTERN':<16} {'BY':<8} {'CREATED':<26} REVOKED")
    for r in rows:
        print(f"{r['id']:<5} {r['pattern']:<16} {r['granted_by']:<8}"
              f" {r['created_at']:<26} {r['revoked_at'] or '-'}")
    return 0


def cmd_grant_revoke(db_path: Path, grant_id: int) -> int:
    from hermes.policy import ApprovalPolicy

    conn = _conn(db_path)
    if ApprovalPolicy.revoke(conn, grant_id):
        print(f"grant #{grant_id} revoked")
        return 0
    print(f"grant #{grant_id} not found or already revoked")
    return 1


def cmd_chat(db_path: Path, one_shot: str | None) -> int:
    """hermes 对话入口（Evolution v3 §6.1）。"""
    import asyncio

    from hermes.brain import Hermes
    from orchestrator.task_manager import TaskManager

    tm = TaskManager(db_path=db_path)
    brain = Hermes(tm)

    async def _once(text: str) -> None:
        reply = await brain.chat(text)
        print(f"hermes> {reply}")

    if one_shot:
        asyncio.run(_once(one_shot))
        return 0

    print("hermes chat — 输入 quit 退出")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in ("quit", "exit", ""):
            return 0
        try:
            asyncio.run(_once(text))
        except Exception as e:
            print(f"hermes> [error] {type(e).__name__}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--db", type=Path, default=None)
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
    p_tr = task_sub.add_parser("retry")
    p_tr.add_argument("task_id")
    p_tc = task_sub.add_parser("cancel")
    p_tc.add_argument("task_id")
    p_ta = task_sub.add_parser("approve")
    p_ta.add_argument("task_id")
    p_ta.add_argument("--notes", default="")
    p_tj = task_sub.add_parser("reject")
    p_tj.add_argument("task_id")
    p_tj.add_argument("--notes", default="")

    p_ev = sub.add_parser("events")
    p_ev.add_argument("--follow", action="store_true")
    p_ev.add_argument("--interval", type=float, default=1.0)
    p_ev.add_argument("--type", dest="event_type", default=None)

    p_grant = sub.add_parser("grant")
    grant_sub = p_grant.add_subparsers(dest="sub", required=True)
    p_gl = grant_sub.add_parser("list")
    p_gl.add_argument("--all", action="store_true")
    p_gr = grant_sub.add_parser("revoke")
    p_gr.add_argument("grant_id", type=int)

    p_chat = sub.add_parser("chat")
    p_chat.add_argument("message", nargs="?", default=None,
                        help="one-shot 模式；缺省进入交互循环")

    args = parser.parse_args()
    args.db = args.db or _default_db()
    if args.command == "status":
        return cmd_status(args.db)
    if args.command == "agent" and args.sub == "list":
        return cmd_agent_list(args.db)
    if args.command == "task" and args.sub == "list":
        return cmd_task_list(args.db, args.status)
    if args.command == "task" and args.sub == "show":
        return cmd_task_show(args.db, args.task_id)
    if args.command == "task" and args.sub == "retry":
        return cmd_task_retry(args.db, args.task_id)
    if args.command == "task" and args.sub == "cancel":
        return cmd_task_cancel(args.db, args.task_id)
    if args.command == "task" and args.sub == "approve":
        return cmd_task_approve(args.db, args.task_id, args.notes)
    if args.command == "task" and args.sub == "reject":
        return cmd_task_reject(args.db, args.task_id, args.notes)
    if args.command == "events":
        return cmd_events(args.db, args.follow, args.interval, args.event_type)
    if args.command == "grant" and args.sub == "list":
        return cmd_grant_list(args.db, args.all)
    if args.command == "grant" and args.sub == "revoke":
        return cmd_grant_revoke(args.db, args.grant_id)
    if args.command == "chat":
        return cmd_chat(args.db, args.message)
    return 2


if __name__ == "__main__":
    sys.exit(main())
