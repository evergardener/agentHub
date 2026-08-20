"""State Writer — 设计文档 §8 / §22.3：唯一事件写库者。

消费 AGENT_EVENTS，按 §5.3 迁移表校验后写入 SQLite：
  - event_id 去重（§17.6）
  - 非法迁移拒绝 + system.audit（§5.3）
  - 条件更新防迟到覆盖（§22.3）

运行：PYTHONPATH=src python -m state.writer
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
    "task.input_required": TaskStatus.BLOCKED,
    "task.completed": TaskStatus.COMPLETED,
    "task.failed": TaskStatus.FAILED,
    "task.reviewed": TaskStatus.REVIEWED,
    "task.accepted": TaskStatus.ACCEPTED,
    "task.cancelled": TaskStatus.CANCELLED,
}


class StateWriter:
    def __init__(self, db_path: str | Path):
        self.conn = init_db(db_path)
        from orchestrator import agent_profile_store

        agent_profile_store.seed_catalog(self.conn)
        self.audit_log: list[dict] = []  # 内存留存，另发 system.audit 事件

    def apply(self, event: dict) -> str:
        """应用单条事件。返回 applied / duplicate / rejected / ignored。"""
        event_type = event.get("event_type", "")
        task_id = event.get("task_id")
        source = event.get("source", "unknown")
        payload = event.get("payload", {})

        # 1. 去重（§17.6）
        try:
            state_store.record_event(self.conn, event, commit=False)
        except state_store.DuplicateEvent:
            # An input-required handler may have committed the event and some
            # interactions before a later interaction failed. Reconcile the
            # idempotent interaction records on redelivery before ACKing.
            if event_type == "task.input_required" and task_id:
                self._persist_interactions(task_id, source, payload)
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
                        commit=False,
                    )
            elif event_type in _EVENT_TO_STATUS and task_id:
                dst = _EVENT_TO_STATUS[event_type]
                state_store.transition_task(
                    self.conn, task_id, dst,
                    result_summary=payload.get("summary"),
                    error_message=payload.get("error"),
                    review=payload.get("review"),
                    commit=False,
                )
                if event_type == "task.started":
                    state_store.add_task_run(
                        self.conn, task_id=task_id, agent_id=source,
                        attempt=payload.get("attempt", 1), status="working",
                        trace_id=event.get("trace_id"),
                        commit=False,
                    )
                if event_type in ("task.completed", "task.failed"):
                    state_store.add_task_run(
                        self.conn, task_id=task_id, agent_id=source,
                        attempt=payload.get("attempt", 1),
                        status=dst.value, trace_id=event.get("trace_id"),
                        error_message=payload.get("error"),
                        commit=False,
                    )
                if event_type == "task.failed":
                    failed = state_store.get_task(self.conn, task_id)
                    if (failed is not None
                            and failed["retry_count"] >= failed["max_retries"]):
                        from state import alert_store

                        alert_store.upsert_alert(
                            self.conn,
                            kind="task_retries_exhausted",
                            severity="critical",
                            source="state-writer",
                            task_id=task_id,
                            detail=payload.get("error") or "task failed",
                        )
                if event_type == "task.input_required":
                    self._persist_interactions(
                        task_id, source, payload)
            elif event_type == "artifact.created" and task_id:
                state_store.add_artifact(
                    self.conn, task_id=task_id, agent_id=source,
                    name=payload.get("name", "?"), path=payload.get("path", ""),
                    sha256=payload.get("sha256", ""),
                    commit=False,
                )
            elif event_type.startswith("agent.") and event_type.endswith(".heartbeat"):
                state_store.update_heartbeat(
                    self.conn, source,
                    lease_ttl_seconds=payload.get("lease_ttl_seconds", 90),
                    endpoint=payload.get("endpoint"),
                    skills=payload.get("skills"),
                    commit=False,
                )
                from orchestrator import agent_profile_store

                agent_profile_store.assign_seed_profile(self.conn, source)
            else:
                self.conn.commit()
                return "ignored"
        except state_store.IllegalTransition as e:
            self.conn.rollback()
            # Rejected attempts remain auditable but are not allowed to poison
            # a future retry of a transient failure.
            try:
                state_store.record_event(self.conn, event)
            except state_store.DuplicateEvent:
                pass
            self._audit(event, f"illegal transition rejected: {e}")
            return "rejected"
        except KeyError as e:
            self.conn.rollback()
            try:
                state_store.record_event(self.conn, event)
            except state_store.DuplicateEvent:
                pass
            self._audit(event, f"unknown task: {e}")
            return "rejected"
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return "applied"

    def _persist_interactions(
        self, task_id: str, agent_id: str, payload: dict
    ) -> None:
        interactions = payload.get("interactions") or []
        if not interactions:
            return
        task = state_store.get_task(self.conn, task_id)
        if task is None or not task["collaboration_id"]:
            return
        from orchestrator import collaboration_store

        binding = collaboration_store.get_current_agent_session(
            self.conn, task_id, agent_id)
        if binding is None:
            adapter_session_id = payload.get("session_id")
            if not adapter_session_id:
                self._audit(
                    {"event_id": None,
                     "event_type": "agent.interaction.requested",
                     "task_id": task_id, "source": agent_id},
                    "session interaction has no durable binding metadata")
                return
            capabilities = payload.get("capabilities") or {}
            binding = collaboration_store.upsert_agent_session(
                self.conn,
                collaboration_id=task["collaboration_id"],
                task_id=task_id,
                agent_id=agent_id,
                adapter_session_id=adapter_session_id,
                native_session_id=payload.get("native_session_id"),
                adapter_instance_id=payload.get("adapter_instance_id"),
                capabilities=capabilities,
                resume_capability=(
                    "native" if payload.get("native_session_id")
                    and capabilities.get("native_resume") is True
                    else "unknown"),
                recovery_state="event_recovered",
                context_snapshot={"objective": task["objective"]},
            )
        collaboration = collaboration_store.get_collaboration(
            self.conn, task["collaboration_id"])
        if collaboration is None:
            return
        for item in interactions:
            saved = collaboration_store.upsert_session_interaction(
                self.conn,
                collaboration_id=task["collaboration_id"],
                task_id=task_id,
                session_binding_id=binding["id"],
                agent_id=agent_id,
                interaction=item,
            )
            if saved["kind"] != "approval" or saved["action_intent_id"]:
                continue
            details = item.get("payload") or {}
            tool_view = details.get("toolView") or {}
            target_paths = tool_view.get("paths") or []
            targets = {
                "workspace": str(self._workspace_root()),
                "nativeSessionId": item.get("nativeSessionId"),
                "callId": details.get("callId"),
                "paths": target_paths,
            }
            if tool_view.get("cwd"):
                targets["path"] = tool_view["cwd"]
            intent = collaboration_store.request_action_intent(
                self.conn,
                collaboration_id=task["collaboration_id"],
                task_id=task_id,
                session_binding_id=binding["id"],
                requested_by_agent_id=agent_id,
                operation=f"agent.tool.{details.get('toolName') or 'unknown'}",
                targets=targets,
                purpose=details.get("reason") or "native agent tool request",
                expected_effects={
                    "toolName": details.get("toolName"),
                    "approvalId": details.get("approvalId"),
                    "toolView": tool_view,
                },
                rollback_plan=None,
                based_on_revision=collaboration["context_revision"],
            )
            collaboration_store.attach_action_intent(
                self.conn, saved["id"], intent["id"])

    @staticmethod
    def _workspace_root() -> Path:
        from common import config as cfg

        return cfg.workspace()

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
    from common import tracing

    tracing.init_tracing("state-writer")
    nats_url = cfg.nats_url()
    db_path = cfg.database_url()  # LAS_DATABASE_URL（pg/sqlite 双后端）
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
