"""Adapter 公共管道 — 设计文档 §9 / §9.1 / §17.7 / §22.5。

提供：
- A2aTaskStore：内存 A2A Task 存储（Phase 1–2 不落库）
- FifoExecutor：单任务串行执行（max_concurrent_tasks=1 默认）
- EventPublisher：NATS best-effort 发布 + 本地暂存重发
- 幂等：idempotency_key -> task_id 去重
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from common.events import Event

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def workspace_root() -> Path:
    return Path(os.environ.get("AGENT_WORKSPACE", Path.home() / "AgentWorkspace"))


# ---------- A2A 数据结构（Phase 1 最小子集） ----------


@dataclass
class A2aTask:
    id: str
    status_state: str            # A2A 状态：submitted/working/input-required/completed/failed/canceled
    objective: str
    idempotency_key: str | None = None
    artifacts: list[dict] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_a2a(self) -> dict:
        return {
            "id": self.id,
            "status": {"state": self.status_state, "timestamp": self.updated_at},
            "artifacts": self.artifacts,
            **({"error": self.error} if self.error else {}),
        }


class A2aTaskStore:
    """内存 Task 存储。Phase 3 起由 State Writer 落 SQLite，此处保持内存态。"""

    def __init__(self) -> None:
        self._tasks: dict[str, A2aTask] = {}
        self._by_idempotency_key: dict[str, str] = {}

    def create(self, task: A2aTask) -> tuple[A2aTask, bool]:
        """按幂等键去重（§22.5）。返回 (task, created_new)。"""
        if task.idempotency_key and task.idempotency_key in self._by_idempotency_key:
            return self._tasks[self._by_idempotency_key[task.idempotency_key]], False
        self._tasks[task.id] = task
        if task.idempotency_key:
            self._by_idempotency_key[task.idempotency_key] = task.id
        return task, True

    def get(self, task_id: str) -> A2aTask | None:
        return self._tasks.get(task_id)

    def update_state(self, task_id: str, state: str, error: str | None = None) -> None:
        t = self._tasks[task_id]
        t.status_state = state
        t.updated_at = now_iso()
        if error is not None:
            t.error = error


# ---------- 事件发布（best-effort + 暂存） ----------


class EventPublisher:
    """NATS 可用则发布；不可用则暂存到本地 JSONL，恢复后可重发（§17.7）。"""

    def __init__(self, source: str, nats_url: str = "nats://127.0.0.1:4222"):
        self.source = source
        self.nats_url = nats_url
        self.spool = workspace_root() / "logs" / "events-pending.jsonl"

    async def publish(self, event_type: str, task_id: str | None,
                      payload: dict, trace_id: str | None = None) -> bool:
        event = Event(
            event_type=event_type, source=self.source,
            task_id=task_id, trace_id=trace_id, payload=payload,
        )
        try:
            import nats  # 延迟导入，无 NATS 时也能运行

            # max_reconnect_attempts=1 + allow_reconnect=False：
            # nats-py 默认重试 60 次，NATS 不在时会挂住调用方（实测确认）。
            nc = await nats.connect(
                self.nats_url,
                connect_timeout=1,
                max_reconnect_attempts=1,
                allow_reconnect=False,
            )
            await nc.jetstream().publish(
                event_type, json.dumps(event.to_dict()).encode("utf-8")
            )
            await nc.close()
            return True
        except Exception:
            self._spool(event)
            return False

    def _spool(self, event: Event) -> None:
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        with self.spool.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")


# ---------- FIFO 执行器（§9.1） ----------


class FifoExecutor:
    """默认单并发：超出并发的请求在内部排队，A2A 状态保持 submitted。"""

    def __init__(self, max_concurrent: int = 1):
        self._sem = asyncio.Semaphore(max_concurrent)

    async def run(self, coro):
        async with self._sem:
            return await coro


# ---------- Artifact ----------


def save_artifact(task_id: str, name: str, content: bytes,
                  artifact_type: str = "file") -> dict:
    """写入 tasks/<id>/artifacts/，返回带 sha256 的 artifact 描述（§22.4）。"""
    out_dir = workspace_root() / "tasks" / task_id / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_bytes(content)
    return {
        "name": name,
        "type": artifact_type,
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "created_at": now_iso(),
        "task_id": task_id,
    }
