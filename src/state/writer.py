"""State Writer — 设计文档 §8 / §22.3：唯一事件写库者。

消费 AGENT_EVENTS，按 §5.3 迁移表校验后写入 SQLite：
  - event_id 去重（§17.6）
  - 非法迁移拒绝 + system.audit（§5.3）
  - 条件更新防迟到覆盖（§22.3）

运行：PYTHONPATH=src python -m state.writer
"""

from __future__ import annotations

import asyncio

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.nats_client import durable_consume, ensure_stream
from state.db import init_db

DURABLE = "state-writer"

# 事件类型 → 目标内部状态
_EVENT_TO_STATUS = {
    "task.assigned": TaskStatus.ASSIGNED,
    "task.started": TaskStatus.WORKING,
    "task.blocked": TaskStatus.BLOCKED,
    "task.completed": TaskStatus.COMPLETED,
    "task.failed": TaskStatus.FAILED,
    "task.reviewed": TaskStatus.REVIEWED,
    "task.accepted": TaskStatus.ACCEPTED,
    "task.cancelled": TaskStatus.CANCELLED,
}


class StateWriter:
    def __init__(self, db_path: str | Path):
        self.conn = init_db(db_path)
        self.audit_log: list[dict] = []  # 内存留存，另发 system.audit 事件

    def apply(self, event: dict) -> str:
        """应用单条事件。返回 applied / duplicate / rejected / ignored。"""
        event_type = event.get("event_type", "")
        task_id = event.get("task_id")
        source = event.get("source", "unknown")
        payload = event.get("payload", {})

        # 1. 去重（§17.6）
        try:
            state_store.record_event(self.conn, event)
        except state_store.DuplicateEvent:
            return "duplicate"

        try:
            # 2. 按事件类型分发
            if event_type == "task.created":
                if task_id and state_store.get_task(self.conn, task_id) is None:
                    state_store.create_task(
                        self.conn, task_id=task_id,
                        objective=payload.get("objective", "(no objective)"),
                        created_by=source, project=payload.get("project"),
                        assigned_to=payload.get("assigned_to"),
                        status=TaskStatus.CREATED,
                    )
            elif event_type in _EVENT_TO_STATUS and task_id:
                dst = _EVENT_TO_STATUS[event_type]
                state_store.transition_task(
                    self.conn, task_id, dst,
                    result_summary=payload.get("summary"),
                    error_message=payload.get("error"),
                    review=payload.get("review"),
                )
                if event_type == "task.started":
                    state_store.add_task_run(
                        self.conn, task_id=task_id, agent_id=source,
                        attempt=payload.get("attempt", 1), status="working",
                        trace_id=event.get("trace_id"),
                    )
                if event_type in ("task.completed", "task.failed"):
                    state_store.add_task_run(
                        self.conn, task_id=task_id, agent_id=source,
                        attempt=payload.get("attempt", 1),
                        status=dst.value, trace_id=event.get("trace_id"),
                        error_message=payload.get("error"),
                    )
            elif event_type == "artifact.created" and task_id:
                state_store.add_artifact(
                    self.conn, task_id=task_id, agent_id=source,
                    name=payload.get("name", "?"), path=payload.get("path", ""),
                    sha256=payload.get("sha256", ""),
                )
            elif event_type.startswith("agent.") and event_type.endswith(".heartbeat"):
                state_store.update_heartbeat(
                    self.conn, source,
                    lease_ttl_seconds=payload.get("lease_ttl_seconds", 90),
                    endpoint=payload.get("endpoint"),
                    skills=payload.get("skills"),
                )
            else:
                return "ignored"
        except state_store.IllegalTransition as e:
            self._audit(event, f"illegal transition rejected: {e}")
            return "rejected"
        except KeyError as e:
            self._audit(event, f"unknown task: {e}")
            return "rejected"
        return "applied"

    def _audit(self, event: dict, reason: str) -> None:
        record = {
            "reason": reason,
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "task_id": event.get("task_id"),
            "source": event.get("source"),
        }
        self.audit_log.append(record)
        # system.audit 事件由调用方（消费者循环）发布，避免此处持有 NATS 连接
        print(f"[state-writer] AUDIT {record}")


async def main() -> None:
    from common import config as cfg

    nats_url = cfg.nats_url()
    db_path = cfg.state_db()
    writer = StateWriter(db_path)
    await ensure_stream(nats_url)

    async def handler(event: dict) -> None:
        result = writer.apply(event)
        if result == "rejected":
            # 非法迁移 → system.audit（§5.3）
            from common.events import Event
            import json
            import nats

            nc = await nats.connect(nats_url, connect_timeout=2,
                                    max_reconnect_attempts=1,
                                    allow_reconnect=False)
            try:
                audit = Event(
                    event_type="system.audit", source="state-writer",
                    task_id=event.get("task_id"),
                    payload={"rejected_event": event.get("event_id"),
                             "event_type": event.get("event_type"),
                             "reason": "illegal transition or unknown task"},
                )
                await nc.jetstream().publish(
                    "system.audit",
                    json.dumps(audit.to_dict()).encode("utf-8"))
            finally:
                await nc.close()

    await durable_consume(DURABLE, handler, nats_url)


if __name__ == "__main__":
    asyncio.run(main())
