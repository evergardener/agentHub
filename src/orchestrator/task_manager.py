"""Task Manager — 设计文档 §11 task_manager.py / Phase 4。

Hermes 的任务控制面：
  create_task / delegate_task / wait_task / review_result / retry_task / cancel_task
状态写入遵守 §22.3：Hermes 只发生命周期命令（经 state_store）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path

from common.ids import idempotency_key as make_idem_key
from common.memory import MemoryService
from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.a2a_client import A2aClient
from state.db import init_db, next_task_id

DEFAULT_DB = Path(
    os.environ.get("AGENT_STATE_DB")
    or Path(os.environ.get("AGENT_WORKSPACE", Path.home() / "AgentWorkspace"))
    / "runtime" / "agent-state.db"
)
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", Path.home() / "AgentWorkspace"))


class TaskManager:
    def __init__(self, db_path: str | Path = DEFAULT_DB,
                 workspace: Path = WORKSPACE,
                 memory: "MemoryService | None" = None):
        self.conn: sqlite3.Connection = init_db(db_path)
        self.workspace = Path(workspace)
        # 长期记忆写方仅限 Hermes（§15.3）；None 时静默跳过。
        # best-effort：记忆服务故障不阻塞任务流。
        self.memory = memory

    # ---------- 创建 ----------

    def create_task(self, objective: str, *, project: str | None = None,
                    parent_id: str | None = None,
                    depends_on: list[str] | None = None,
                    priority: str = "normal",
                    timeout_seconds: int = 1800,
                    max_retries: int = 2,
                    context: str | None = None) -> str:
        task_id = next_task_id(self.conn)
        root_id = parent_id or task_id
        if parent_id:
            parent = state_store.get_task(self.conn, parent_id)
            if parent:
                root_id = parent["root_id"]
        state_store.create_task(
            self.conn, task_id=task_id, objective=objective,
            created_by="hermes", project=project, parent_id=parent_id,
            root_id=root_id, priority=priority, depends_on=depends_on,
            timeout_seconds=timeout_seconds, max_retries=max_retries,
            idempotency_key=make_idem_key(task_id, 1),
            status=TaskStatus.CREATED,
        )
        # Task Workspace（§3.8）
        tdir = self.workspace / "tasks" / task_id
        (tdir / "input").mkdir(parents=True, exist_ok=True)
        (tdir / "artifacts").mkdir(exist_ok=True)
        (tdir / "logs").mkdir(exist_ok=True)
        (tdir / "task.yaml").write_text(
            f"id: {task_id}\nparent_id: {parent_id}\nroot_id: {root_id}\n"
            f"project: {project}\nobjective: |\n  {objective}\n",
            encoding="utf-8",
        )
        (tdir / "context.md").write_text(context or f"# Task {task_id}\n\n{objective}\n",
                                         encoding="utf-8")
        # depends_on 门控（§5.3）：前置任务全部 accepted 才可 queued
        if depends_on and not self._deps_satisfied(depends_on):
            pass  # 保持 created，等待 promote_dependents
        else:
            state_store.transition_task(self.conn, task_id, TaskStatus.QUEUED)
        return task_id

    def _deps_satisfied(self, depends_on: list[str]) -> bool:
        for dep in depends_on:
            row = state_store.get_task(self.conn, dep)
            if row is None or row["status"] != "accepted":
                return False
        return True

    def promote_dependents(self, accepted_task_id: str) -> list[str]:
        """某任务 accepted 后，把依赖已满足的 created 子任务推进 queued。"""
        promoted = []
        rows = self.conn.execute(
            "SELECT id, depends_on_json FROM tasks WHERE status = 'created';"
        ).fetchall()
        for r in rows:
            deps = json.loads(r["depends_on_json"] or "[]")
            if deps and self._deps_satisfied(deps):
                state_store.transition_task(self.conn, r["id"], TaskStatus.QUEUED)
                promoted.append(r["id"])
        return promoted

    # ---------- 委派 ----------

    async def delegate_task(self, task_id: str, endpoint: str,
                            agent_id: str, attempt: int = 1) -> asyncio.Task:
        """标记 assigned 并后台发起 A2A 调用；返回 asyncio.Task（不阻塞）。"""
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        state_store.transition_task(self.conn, task_id, TaskStatus.ASSIGNED)
        self.conn.execute(
            "UPDATE tasks SET assigned_to = ?, updated_at = datetime('now')"
            " WHERE id = ?;", (agent_id, task_id),
        )
        self.conn.commit()

        # 异步 A2A：send 立即返回，60s 足够；结果经事件/轮询对齐（v3 M1）
        client = A2aClient.for_agent(agent_id, endpoint, timeout=60)

        async def _call() -> None:
            try:
                await client.send_message(
                    row["objective"],
                    idempotency_key=make_idem_key(task_id, attempt),
                    trace_id=f"trace-{row['root_id']}",
                    task_id=task_id,
                )
                # 结果经 NATS → State Writer 落库；此处不直接写状态
            except Exception:
                # A2A 调用本身失败（Adapter 不可达等）：走重试流程
                try:
                    state_store.transition_task(
                        self.conn, task_id, TaskStatus.FAILED,
                        error_message="a2a call failed",
                    )
                except Exception:
                    pass

        return asyncio.create_task(_call())

    # ---------- 等待（event-driven，NATS 不可用时降级 DB 轮询） ----------

    async def wait_task(self, task_id: str, timeout: float = 600.0,
                        nats_url: str | None = None) -> str:
        """等待任务到达 completed/failed/cancelled。返回最终状态。"""
        terminal = {"completed", "failed", "cancelled", "accepted"}

        async def _db_status() -> str | None:
            row = state_store.get_task(self.conn, task_id)
            return row["status"] if row else None

        url = nats_url or os.environ.get("NATS_URL")
        if url:
            try:
                return await self._wait_via_nats(task_id, url, terminal, timeout)
            except Exception:
                pass  # 降级 DB 轮询

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            status = await _db_status()
            if status in terminal:
                return status
            await asyncio.sleep(0.5)
        raise TimeoutError(f"wait_task {task_id} exceeded {timeout}s")

    async def _wait_via_nats(self, task_id: str, url: str,
                             terminal: set[str], timeout: float) -> str:
        import nats

        nc = await nats.connect(url, connect_timeout=2,
                                max_reconnect_attempts=1,
                                allow_reconnect=False)
        try:
            # 先查一次 DB，避免等待已结束的任务
            row = state_store.get_task(self.conn, task_id)
            if row and row["status"] in terminal:
                return row["status"]
            sub = await nc.subscribe("task.*")
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await sub.next_msg(timeout=1)
                except nats.errors.TimeoutError:
                    row = state_store.get_task(self.conn, task_id)
                    if row and row["status"] in terminal:
                        return row["status"]
                    continue
                event = json.loads(msg.data.decode("utf-8"))
                if event.get("task_id") != task_id:
                    continue
                await asyncio.sleep(0.2)  # 等 State Writer 落库
                row = state_store.get_task(self.conn, task_id)
                if row and row["status"] in terminal:
                    return row["status"]
        finally:
            await nc.close()
        raise TimeoutError(f"wait_task {task_id} exceeded {timeout}s")

    # ---------- Review（§5.3） ----------

    def review_result(self, task_id: str, *, approved: bool,
                      notes: str = "", reviewer: str = "hermes") -> str:
        """completed → reviewed → accepted / working(返工)。返回新状态。"""
        review = {"reviewer": reviewer,
                  "verdict": "approved" if approved else "rejected",
                  "notes": notes}
        state_store.transition_task(self.conn, task_id, TaskStatus.REVIEWED,
                                    review=review)
        if approved:
            state_store.transition_task(self.conn, task_id, TaskStatus.ACCEPTED)
            self.promote_dependents(task_id)  # 解锁依赖本任务的后续任务
            self._retain_outcome(task_id, notes)
            return "accepted"
        state_store.transition_task(self.conn, task_id, TaskStatus.WORKING)
        return "working"  # 返工：调用方应重新 delegate（attempt+1）

    # ---------- 审批（§5.4 input-required 闭环） ----------

    def approve_task(self, task_id: str, *, notes: str = "") -> str:
        """blocked（A2A input-required）→ working：用户批准继续。"""
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        if row["status"] != TaskStatus.BLOCKED.value:
            raise state_store.IllegalTransition(
                task_id, row["status"], TaskStatus.WORKING.value)
        state_store.transition_task(self.conn, task_id, TaskStatus.WORKING,
                                    review={"reviewer": "user",
                                            "verdict": "approved",
                                            "notes": notes})
        return "working"

    def reject_task(self, task_id: str, *, notes: str = "") -> str:
        """blocked → cancelled：用户拒绝，级联取消后代。"""
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        if row["status"] != TaskStatus.BLOCKED.value:
            raise state_store.IllegalTransition(
                task_id, row["status"], TaskStatus.CANCELLED.value)
        self.cancel_task(task_id)
        return "cancelled"

    # ---------- 长期记忆（Hermes 唯一写方，§15.3） ----------

    def _retain_outcome(self, task_id: str, notes: str) -> None:
        """任务被接受后把结果摘要写入长期记忆；失败静默（best-effort）。"""
        if self.memory is None:
            return
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            return
        scope = f"project:{row['project']}" if row["project"] else "system"
        content = (
            f"任务 {task_id} 已完成并验收。\n"
            f"目标：{row['objective']}\n"
            f"执行者：{row['assigned_to'] or '-'}\n"
            + (f"评审备注：{notes}\n" if notes else "")
        )
        try:
            self.memory.retain(content, scope,
                               {"task_id": task_id, "kind": "task_outcome"})
        except Exception:
            pass  # 记忆服务不可用不阻塞任务流

    # ---------- 重试 / 取消 ----------

    def retry_task(self, task_id: str) -> str:
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        if row["status"] != "failed":
            raise state_store.IllegalTransition(task_id, row["status"], "retry_pending")
        if row["retry_count"] > row["max_retries"]:
            raise RuntimeError(f"task {task_id} retries exhausted")
        state_store.transition_task(self.conn, task_id, TaskStatus.RETRY_PENDING)
        state_store.transition_task(self.conn, task_id, TaskStatus.QUEUED)
        return "queued"

    def cancel_task(self, task_id: str) -> int:
        """取消任务并级联取消全部后代任务。返回取消数量。"""
        cancelled = 0

        def _cancel(tid: str) -> None:
            nonlocal cancelled
            row = state_store.get_task(self.conn, tid)
            if row is None or row["status"] in ("accepted", "cancelled"):
                return
            # failed 终态（重试耗尽）也允许取消
            children = self.conn.execute(
                "SELECT id FROM tasks WHERE parent_id = ?;", (tid,)).fetchall()
            state_store.transition_task(self.conn, tid, TaskStatus.CANCELLED)
            cancelled += 1
            for child in children:
                _cancel(child["id"])

        _cancel(task_id)
        return cancelled

    # ---------- 查询 ----------

    def list_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agents ORDER BY id;").fetchall()

    def close(self) -> None:
        self.conn.close()
