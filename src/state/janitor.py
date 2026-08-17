"""Janitor — 对账组件（设计文档 §17.5）。

独立进程 ai.janitor，每 60 秒扫描 SQLite：
1. working 任务但 Agent 租约过期 → 按策略 requeue / fail
2. working 任务超过 timeout_seconds 无事件 → failed(timeout_swept)
3. parent 已 cancelled 但 child 非终态 → 级联取消
4. artifacts 记录的文件缺失 → system.alert（不删记录）

只依据 SQLite 工作，是"事实源驱动"的最后兜底。
运行：PYTHONPATH=src python -m state.janitor
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from common import config as cfg
from common.models import TaskStatus
from orchestrator import state_store
from state.db import CST, init_db

SWEEP_INTERVAL = float(os.environ.get("JANITOR_INTERVAL", "60"))


class Janitor:
    def __init__(self, db_path: str | Path | None = None):
        self.conn: sqlite3.Connection = init_db(db_path or cfg.state_db())
        self.alerts: list[dict] = []

    def sweep(self) -> dict:
        stats = {"requeued": 0, "failed_timeout": 0,
                 "cascade_cancelled": 0, "artifact_alerts": 0}
        self._sweep_dead_leases(stats)
        self._sweep_timeouts(stats)
        self._sweep_cascade(stats)
        self._sweep_artifacts(stats)
        return stats

    # 1. 租约过期
    def _sweep_dead_leases(self, stats: dict) -> None:
        now = datetime.now(CST).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT t.id AS task_id, t.assigned_to, a.lease_expires_at"
            " FROM tasks t LEFT JOIN agents a ON a.id = t.assigned_to"
            " WHERE t.status = 'working';",
        ).fetchall()
        for r in rows:
            lease = r["lease_expires_at"]
            if lease is not None and lease >= now:
                continue  # 租约有效
            policy = "requeue"  # 默认策略（agents.yaml 的 on_lease_expired 由 Hermes 侧使用）
            try:
                state_store.transition_task(
                    self.conn, r["task_id"], TaskStatus.FAILED,
                    error_message="worker lease expired (janitor)")
                if policy == "requeue":
                    state_store.transition_task(
                        self.conn, r["task_id"], TaskStatus.RETRY_PENDING)
                    state_store.transition_task(
                        self.conn, r["task_id"], TaskStatus.QUEUED)
                    stats["requeued"] += 1
                else:
                    stats["failed_timeout"] += 1
                self._alert("lease_expired", r["task_id"], r["assigned_to"])
            except state_store.IllegalTransition:
                pass

    # 2. 执行超时
    def _sweep_timeouts(self, stats: dict) -> None:
        now = datetime.now(CST)
        rows = self.conn.execute(
            "SELECT id, started_at, timeout_seconds FROM tasks"
            " WHERE status = 'working' AND started_at IS NOT NULL;",
        ).fetchall()
        for r in rows:
            started = datetime.fromisoformat(r["started_at"])
            limit = r["timeout_seconds"] or 1800
            if (now - started).total_seconds() > limit:
                try:
                    state_store.transition_task(
                        self.conn, r["id"], TaskStatus.FAILED,
                        error_message="timeout_swept")
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
            if r["path"] and not Path(r["path"]).exists():
                stats["artifact_alerts"] += 1
                self._alert("artifact_missing", r["task_id"], r["path"])

    def _alert(self, kind: str, task_id: str | None, detail: str | None) -> None:
        alert = {"kind": kind, "task_id": task_id, "detail": detail,
                 "ts": datetime.now(CST).isoformat(timespec="seconds")}
        self.alerts.append(alert)
        print(f"[janitor] ALERT {alert}")


async def main() -> None:
    janitor = Janitor()
    while True:
        janitor.sweep()
        await asyncio.sleep(SWEEP_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
