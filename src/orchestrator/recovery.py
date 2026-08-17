"""启动恢复 — 设计文档 §17.2。

Hermes 集成层启动时，在接收新任务之前执行：
1. 查 SQLite 所有非终态任务
2. assigned/working 任务：经 A2A 按 task_id 查 Worker 真实状态并对齐
3. Worker 不可达：查租约，过期则按 §17.4 策略处置
4. queued/retry_pending 任务：保持等待调度
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from common.models import A2A_STATE_MAP, TaskStatus
from orchestrator import state_store
from orchestrator.a2a_client import A2aClient, A2aError
from state.db import CST

NON_TERMINAL = ("created", "queued", "assigned", "working", "blocked",
                "retry_pending", "completed", "reviewed")
_A2A_TO_INTERNAL = {v: k for k, v in A2A_STATE_MAP.items()}


async def recover(conn: sqlite3.Connection, endpoints: dict[str, str],
                  on_lease_expired: str = "requeue") -> dict:
    """执行恢复流程，返回统计。endpoints: agent_id -> base_url。"""
    stats = {"aligned": 0, "waiting": 0, "requeued": 0, "failed": 0}
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(NON_TERMINAL))});",
        NON_TERMINAL,
    ).fetchall()

    for row in rows:
        task_id = row["id"]
        status = row["status"]
        if status not in ("assigned", "working"):
            continue  # queued/retry_pending 等待调度；completed/reviewed 等 review

        agent_id = row["assigned_to"]
        endpoint = endpoints.get(agent_id or "")
        if not endpoint:
            stats["waiting"] += 1
            continue

        try:
            remote = await A2aClient(endpoint, timeout=5).get_task(task_id)
        except (A2aError, Exception):  # Worker 不可达
            if _lease_expired(conn, agent_id):
                _resolve_dead_worker(conn, task_id, on_lease_expired, stats)
            else:
                stats["waiting"] += 1
            continue

        # 对齐 Worker 真实状态
        remote_state = remote.get("status", {}).get("state")
        target = _A2A_TO_INTERNAL.get(remote_state)
        if target is None:
            stats["waiting"] += 1
            continue
        current = TaskStatus(status)
        if target == current:
            stats["aligned"] += 1
            continue
        try:
            state_store.transition_task(conn, task_id, target)
            stats["aligned"] += 1
        except state_store.IllegalTransition:
            stats["waiting"] += 1  # 无法对齐，留给人工/Janitor
    return stats


def _lease_expired(conn: sqlite3.Connection, agent_id: str | None) -> bool:
    if not agent_id:
        return True
    row = conn.execute(
        "SELECT lease_expires_at FROM agents WHERE id = ?;", (agent_id,)
    ).fetchone()
    if row is None or row["lease_expires_at"] is None:
        return True  # 从未见过心跳 → 视为失联
    return datetime.now(CST).isoformat(timespec="seconds") > row["lease_expires_at"]


def _resolve_dead_worker(conn: sqlite3.Connection, task_id: str,
                         policy: str, stats: dict) -> None:
    if policy == "fail":
        try:
            state_store.transition_task(conn, task_id, TaskStatus.FAILED,
                                        error_message="worker lease expired")
            stats["failed"] += 1
        except state_store.IllegalTransition:
            stats["waiting"] += 1
        return
    # 默认 requeue：working → failed → retry_pending → queued（§17.4）
    try:
        state_store.transition_task(conn, task_id, TaskStatus.FAILED,
                                    error_message="worker lease expired")
        state_store.transition_task(conn, task_id, TaskStatus.RETRY_PENDING)
        state_store.transition_task(conn, task_id, TaskStatus.QUEUED)
        stats["requeued"] += 1
    except state_store.IllegalTransition:
        stats["waiting"] += 1
