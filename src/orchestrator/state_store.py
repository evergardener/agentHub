"""State Store — SQLite CRUD 与状态迁移执行（设计文档 §11 state_store.py / §22.3）。

写入纪律：
- State Writer（事件驱动状态迁移）与 Hermes（生命周期命令）都经过本模块，
  本模块统一执行 §5.3 迁移表校验 + 条件更新（乐观并发）。
- Worker / Adapter 不得接触本模块。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from common.models import TaskStatus, is_legal_transition
from state.db import now_iso


class IllegalTransition(RuntimeError):
    def __init__(self, task_id: str, src: str, dst: str):
        super().__init__(f"illegal transition {src} -> {dst} for {task_id}")
        self.task_id, self.src, self.dst = task_id, src, dst


class DuplicateEvent(RuntimeError):
    pass


# ---------- Hermes 生命周期命令（§22.3 白名单） ----------


def create_task(conn: sqlite3.Connection, *, task_id: str, objective: str,
                created_by: str, project: str | None = None,
                parent_id: str | None = None, root_id: str | None = None,
                priority: str = "normal", assigned_to: str | None = None,
                depends_on: list[str] | None = None,
                timeout_seconds: int | None = None,
                max_retries: int = 2, idempotency_key: str | None = None,
                status: str = TaskStatus.QUEUED) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO tasks (id, parent_id, root_id, project, created_by,"
        " assigned_to, status, priority, objective, depends_on_json,"
        " timeout_seconds, max_retries, idempotency_key, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);",
        (task_id, parent_id, root_id or task_id, project, created_by,
         assigned_to, status, priority, objective,
         json.dumps(depends_on or []), timeout_seconds, max_retries,
         idempotency_key, ts, ts),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, status: str | None = None,
               limit: int = 50) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?;",
            (status, limit)).fetchall()
    return conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?;", (limit,)).fetchall()


def transition_task(conn: sqlite3.Connection, task_id: str,
                    dst: TaskStatus, *,
                    result_summary: str | None = None,
                    error_message: str | None = None,
                    review: dict | None = None) -> None:
    """按 §5.3 校验并执行迁移；条件更新防止迟到事件覆盖（§22.3）。"""
    row = get_task(conn, task_id)
    if row is None:
        raise KeyError(f"task not found: {task_id}")
    src = TaskStatus(row["status"])
    if src == dst:
        return  # 重复事件，幂等
    if not is_legal_transition(src, dst):
        raise IllegalTransition(task_id, src.value, dst.value)
    ts = now_iso()
    extra = ""
    params: list = [dst.value, ts]
    if dst == TaskStatus.WORKING:
        extra += ", started_at = COALESCE(started_at, ?)"
        params.append(ts)
    if dst in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        extra += ", completed_at = ?"
        params.append(ts)
    if dst == TaskStatus.FAILED:
        extra += ", retry_count = retry_count + 1"
    if result_summary is not None:
        extra += ", result_summary = ?"
        params.append(result_summary)
    if error_message is not None:
        extra += ", error_message = ?"
        params.append(error_message)
    if review is not None:
        extra += ", review_json = ?"
        params.append(json.dumps(review, ensure_ascii=False))
    params += [task_id, src.value]
    cur = conn.execute(
        f"UPDATE tasks SET status = ?, updated_at = ?{extra}"
        " WHERE id = ? AND status = ?;",
        params,
    )
    conn.commit()
    if cur.rowcount == 0:
        raise IllegalTransition(task_id, src.value, dst.value)  # 并发下状态已变


# ---------- State Writer 专用 ----------


def record_event(conn: sqlite3.Connection, event: dict) -> None:
    """登记事件；event_id 重复时抛 DuplicateEvent（§17.6 去重）。"""
    try:
        conn.execute(
            "INSERT INTO events (id, subject, task_id, agent_id, event_type,"
            " payload_json, created_at) VALUES (?,?,?,?,?,?,?);",
            (event["event_id"], event["event_type"], event.get("task_id"),
             event.get("source"), event["event_type"],
             json.dumps(event.get("payload", {}), ensure_ascii=False),
             event.get("timestamp", now_iso())),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()  # 失败 INSERT 会留下持锁的开放事务，必须回滚
        raise DuplicateEvent(event["event_id"])


def add_task_run(conn: sqlite3.Connection, *, task_id: str, agent_id: str,
                 attempt: int, status: str, trace_id: str | None = None,
                 error_message: str | None = None) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO task_runs (id, task_id, agent_id, attempt, status,"
        " started_at, trace_id, error_message) VALUES (?,?,?,?,?,?,?,?);",
        (f"R-{uuid.uuid4().hex[:12]}", task_id, agent_id, attempt, status,
         ts, trace_id, error_message),
    )
    conn.commit()


def add_artifact(conn: sqlite3.Connection, *, task_id: str, agent_id: str,
                 name: str, path: str, sha256: str,
                 artifact_type: str = "file") -> None:
    conn.execute(
        "INSERT INTO artifacts (id, task_id, agent_id, type, name, path,"
        " sha256, created_at) VALUES (?,?,?,?,?,?,?,?);",
        (f"A-{uuid.uuid4().hex[:12]}", task_id, agent_id, artifact_type,
         name, path, sha256, now_iso()),
    )
    conn.commit()


def upsert_agent(conn: sqlite3.Connection, *, agent_id: str, role: str = "worker",
                 endpoint: str | None = None, protocol: str = "a2a",
                 status: str = "online") -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO agents (id, role, endpoint, protocol, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at;",
        (agent_id, role, endpoint, protocol, status, ts, ts),
    )
    conn.commit()


def update_heartbeat(conn: sqlite3.Connection, agent_id: str,
                     lease_ttl_seconds: int = 90,
                     endpoint: str | None = None,
                     skills: list[str] | None = None) -> None:
    """更新心跳租约（§17.4）；携带 endpoint/skills 时一并登记（v3 M2 发现注册）。"""
    from datetime import datetime, timedelta as td

    from state.db import CST

    now = datetime.now(CST)
    ts = now.isoformat(timespec="seconds")
    lease = (now + td(seconds=lease_ttl_seconds)).isoformat(timespec="seconds")
    upsert_agent(conn, agent_id=agent_id, endpoint=endpoint)
    conn.execute(
        "UPDATE agents SET last_seen_at = ?, lease_expires_at = ?,"
        " status = 'online', updated_at = ? WHERE id = ?;",
        (ts, lease, ts, agent_id),
    )
    if endpoint:
        conn.execute(
            "UPDATE agents SET endpoint = ? WHERE id = ?;", (endpoint, agent_id))
    if skills is not None:
        conn.execute(
            "UPDATE agents SET skills_json = ? WHERE id = ?;",
            (json.dumps(skills, ensure_ascii=False), agent_id))
    conn.commit()
