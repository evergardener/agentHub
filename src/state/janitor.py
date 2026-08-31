"""Janitor — 对账组件（设计文档 §17.5）。

独立进程 ai.janitor，每 60 秒扫描 SQLite：
1. working 任务但 Agent 租约过期 → 按策略 requeue / fail
2. working 任务的当前连续执行区间超过 timeout_seconds → failed(timeout_swept)
3. parent 已 cancelled 但 child 非终态 → 级联取消
4. artifacts 记录的文件缺失 → system.alert（不删记录）

只依据 SQLite 工作，是"事实源驱动"的最后兜底。
运行：PYTHONPATH=src python -m state.janitor
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from common import config as cfg
from common.models import TaskStatus
from orchestrator import state_store
from state import alert_store
from state.db import CST, init_db

SWEEP_INTERVAL = float(os.environ.get("JANITOR_INTERVAL", "60"))
MESSAGE_DELIVERY_TIMEOUT_SECONDS = max(
    60.0,
    float(os.environ.get("HERMES_MESSAGE_DELIVERY_TIMEOUT_SECONDS", "300")),
)


class Janitor:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        artifact_roots: tuple[Path, ...] | None = None,
    ):
        self.conn: sqlite3.Connection = init_db(db_path)  # None → LAS_DATABASE_URL
        self.alerts: list[dict] = []
        self.artifact_roots = tuple(
            root.expanduser().resolve(strict=False)
            for root in (artifact_roots or cfg.artifact_roots())
        )

    def sweep(self) -> dict:
        stats = {"requeued": 0, "failed_timeout": 0,
                 "cascade_cancelled": 0, "artifact_alerts": 0,
                 "artifact_resolved": 0, "artifact_ignored": 0,
                 "message_delivery_failed": 0}
        self._sweep_queued_messages(stats)
        self._sweep_dead_leases(stats)
        self._sweep_timeouts(stats)
        self._sweep_cascade(stats)
        self._sweep_artifacts(stats)
        return stats

    def _sweep_queued_messages(self, stats: dict) -> None:
        """Fail Hermes messages that no delivery owner claimed in time."""
        cutoff = (
            datetime.now(CST)
            - timedelta(seconds=MESSAGE_DELIVERY_TIMEOUT_SECONDS)
        ).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT o.id AS outbox_id, o.message_id"
            " FROM supervision_outbox o JOIN conversation_messages m"
            " ON m.id = o.message_id"
            " WHERE o.event_type = 'conversation.user_message'"
            " AND o.status = 'pending' AND o.attempts = 0"
            " AND o.created_at <= ? AND m.delivery_status = 'queued';",
            (cutoff,),
        ).fetchall()
        if not rows:
            return
        from orchestrator import collaboration_store

        for row in rows:
            changed = self.conn.execute(
                "UPDATE supervision_outbox SET status = 'failed',"
                " lease_until = NULL WHERE id = ? AND status = 'pending'"
                " AND attempts = 0;",
                (row["outbox_id"],),
            )
            if changed.rowcount != 1:
                continue
            collaboration_store.update_message_delivery_status(
                self.conn,
                message_id=row["message_id"],
                delivery_status="failed",
                expected_statuses={"queued"},
                reason="delivery_not_claimed_before_timeout",
                commit=False,
            )
            stats["message_delivery_failed"] += 1
        self.conn.commit()

    # 1. 租约过期
    def _sweep_dead_leases(self, stats: dict) -> None:
        now = datetime.now(CST).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT t.id AS task_id, t.assigned_to, t.status, t.updated_at,"
            " t.retry_count, t.collaboration_id, a.lease_expires_at"
            " FROM tasks t LEFT JOIN agents a ON a.id = t.assigned_to"
            " WHERE t.status IN ('assigned','working','blocked');",
        ).fetchall()
        for r in rows:
            lease = r["lease_expires_at"]
            if lease is not None and lease >= now:
                continue  # 租约有效
            policy = "requeue"  # 默认策略（agents.yaml 的 on_lease_expired 由 Hermes 侧使用）
            try:
                error = "worker lease expired (janitor)"
                state_store.transition_task(
                    self.conn, r["task_id"], TaskStatus.FAILED,
                    error_message=error, commit=False)
                failure_event_id = (
                    f"janitor-lease:{r['task_id']}:{r['updated_at']}")
                state_store.record_event(self.conn, {
                    "event_id": failure_event_id,
                    "event_type": "task.failed",
                    "task_id": r["task_id"],
                    "source": "janitor",
                    "payload": {
                        "status_from": r["status"],
                        "status_to": "failed",
                        "attempt": int(r["retry_count"] or 0) + 1,
                        "error": error,
                        "reason": "worker_lease_expired",
                    },
                }, commit=False)
                state_store.add_task_run(
                    self.conn, task_id=r["task_id"],
                    agent_id=r["assigned_to"] or "unknown",
                    attempt=int(r["retry_count"] or 0) + 1,
                    status="failed", error_message=error, commit=False)
                from orchestrator import collaboration_store, supervision_store

                collaboration_store.close_task_interactions(
                    self.conn, r["task_id"], reason=error, commit=False)
                binding = self.conn.execute(
                    "SELECT id FROM agent_session_bindings"
                    " WHERE task_id = ? AND agent_id = ? AND is_current = 1;",
                    (r["task_id"], r["assigned_to"]),
                ).fetchone()
                if binding is not None:
                    collaboration_store.update_agent_session_status(
                        self.conn, binding["id"], status="interrupted",
                        recovery_state="worker_lease_expired", error=error,
                        commit=False)
                if r["collaboration_id"]:
                    collaboration_store.sync_phase_from_tasks(
                        self.conn, r["collaboration_id"], commit=False)
                # Queue a terminal wake before any automatic requeue changes
                # the current task status back to queued.
                supervision_store.sync_task(
                    self.conn, r["task_id"], commit=False)
                if policy == "requeue":
                    state_store.transition_task(
                        self.conn, r["task_id"], TaskStatus.RETRY_PENDING,
                        commit=False)
                    state_store.transition_task(
                        self.conn, r["task_id"], TaskStatus.QUEUED,
                        commit=False)
                    if r["collaboration_id"]:
                        collaboration_store.sync_phase_from_tasks(
                            self.conn, r["collaboration_id"], commit=False)
                    stats["requeued"] += 1
                else:
                    stats["failed_timeout"] += 1
                self.conn.commit()
                self._alert("lease_expired", r["task_id"], r["assigned_to"])
            except state_store.IllegalTransition:
                self.conn.rollback()

    # 2. 执行超时
    def _sweep_timeouts(self, stats: dict) -> None:
        now = datetime.now(CST)
        rows = self.conn.execute(
            "SELECT id, updated_at, timeout_seconds FROM tasks"
            " WHERE status = 'working';",
        ).fetchall()
        for r in rows:
            working_since = datetime.fromisoformat(r["updated_at"])
            if working_since.tzinfo is None:
                working_since = working_since.replace(tzinfo=CST)
            limit = r["timeout_seconds"] or 1800
            if (now - working_since).total_seconds() > limit:
                try:
                    state_store.transition_task(
                        self.conn, r["id"], TaskStatus.FAILED,
                        error_message="timeout_swept",
                        expected_updated_at=r["updated_at"])
                    stats["failed_timeout"] += 1
                    self._alert("timeout_swept", r["id"], None)
                except state_store.IllegalTransition:
                    pass

    # 3. 级联取消兜底
    def _sweep_cascade(self, stats: dict) -> None:
        rows = self.conn.execute(
            "SELECT c.id FROM tasks c JOIN tasks p ON c.parent_id = p.id"
            " WHERE p.status = 'cancelled'"
            " AND c.status NOT IN ('accepted','cancelled');",
        ).fetchall()
        for r in rows:
            try:
                state_store.transition_task(
                    self.conn, r["id"], TaskStatus.CANCELLED)
                stats["cascade_cancelled"] += 1
            except state_store.IllegalTransition:
                pass

    # 4. Artifact 文件缺失
    def _sweep_artifacts(self, stats: dict) -> None:
        rows = self.conn.execute("SELECT id, task_id, path FROM artifacts;").fetchall()
        for r in rows:
            if not r["path"]:
                continue
            path = Path(r["path"]).expanduser().resolve(strict=False)
            roots = getattr(self, "artifact_roots", None) or cfg.artifact_roots()
            managed = any(path == root or root in path.parents for root in roots)
            if not managed:
                stats["artifact_ignored"] += 1
                if alert_store.resolve_condition(
                        self.conn, kind="artifact_missing", task_id=r["task_id"],
                        detail=r["path"], source="janitor:outside-managed-roots"):
                    stats["artifact_resolved"] += 1
            elif not path.exists():
                stats["artifact_alerts"] += 1
                self._alert("artifact_missing", r["task_id"], r["path"])
            elif alert_store.resolve_condition(
                    self.conn, kind="artifact_missing", task_id=r["task_id"],
                    detail=r["path"], source="janitor"):
                stats["artifact_resolved"] += 1

    def _alert(self, kind: str, task_id: str | None, detail: str | None) -> None:
        severity = ("critical" if kind == "timeout_swept" else "warning")
        alert = {"kind": kind, "task_id": task_id, "detail": detail,
                 "severity": severity,
                 "ts": datetime.now(CST).isoformat(timespec="seconds")}
        self.alerts.append(alert)
        alert_store.upsert_alert(
            self.conn, kind=kind, severity=severity, source="janitor",
            task_id=task_id, detail=detail)
        print(f"[janitor] ALERT {alert}")


async def main() -> None:
    janitor = Janitor()
    while True:
        janitor.sweep()
        await asyncio.sleep(SWEEP_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
