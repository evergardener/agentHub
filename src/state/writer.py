"""State Writer — 设计文档 §8 / §22.3：唯一事件写库者。

消费 AGENT_EVENTS，按 §5.3 迁移表校验后写入 SQLite：
  - event_id 去重（§17.6）
  - 非法迁移拒绝 + system.audit（§5.3）
  - 条件更新防迟到覆盖（§22.3）

运行：PYTHONPATH=src python -m state.writer
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.nats_client import durable_consume, ensure_stream
from state.db import init_db

DURABLE = "state-writer"
MAX_CONVERSATION_RESULT_CHARS = 200_000


def _bounded_conversation_result(text: str) -> str:
    if len(text) <= MAX_CONVERSATION_RESULT_CHARS:
        return text
    marker = (
        "\n\n…\n[agentHub：结果事件超过 200000 字符，"
        "State Writer 已按安全上限截断]"
    )
    return text[:MAX_CONVERSATION_RESULT_CHARS - len(marker)].rstrip() + marker

# 事件类型 → 目标内部状态
_EVENT_TO_STATUS = {
    "task.assigned": TaskStatus.ASSIGNED,
    "task.started": TaskStatus.WORKING,
    "task.blocked": TaskStatus.BLOCKED,
    "task.input_required": TaskStatus.BLOCKED,
    "task.completed": TaskStatus.AWAITING_ACCEPTANCE,
    "task.failed": TaskStatus.FAILED,
    "task.reviewed": TaskStatus.REVIEWED,
    "task.cancelled": TaskStatus.CANCELLED,
}


class StateWriter:
    def __init__(self, db_path: str | Path, agents_path: Path | None = None):
        self.db_target = db_path
        self.conn = init_db(db_path)
        from hermes.tools import load_agents

        self.agent_policies = load_agents(agents_path)
        from orchestrator import agent_profile_store

        agent_profile_store.seed_catalog(self.conn)
        self.audit_log: list[dict] = []  # 内存留存，另发 system.audit 事件

    @staticmethod
    def _is_connection_failure(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, BrokenPipeError, EOFError)):
            return True
        try:
            import psycopg

            return isinstance(
                exc, (psycopg.OperationalError, psycopg.InterfaceError))
        except ImportError:
            return False

    def reconnect(self) -> None:
        """Replace a broken DB connection; JetStream redelivery does the retry."""
        replacement = init_db(self.db_target)
        previous = self.conn
        self.conn = replacement
        try:
            previous.close()
        except Exception:
            pass

    def apply_resilient(self, event: dict) -> str:
        """Apply once; reconnect broken PostgreSQL transport, then NAK upstream."""
        try:
            return self.apply(event)
        except Exception as exc:
            if self._is_connection_failure(exc):
                try:
                    self.reconnect()
                except Exception:
                    # Database may still be down. The next redelivery retries
                    # reconnect; the original failure remains visible to NATS.
                    pass
            raise

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
                task = state_store.get_task(self.conn, task_id)
                if task is not None and task["collaboration_id"]:
                    from orchestrator import collaboration_store

                    collaboration_store.sync_phase_from_tasks(
                        self.conn, task["collaboration_id"], commit=False)
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
                        status=("completed" if event_type == "task.completed"
                                else dst.value),
                        trace_id=event.get("trace_id"),
                        error_message=payload.get("error"),
                        commit=False,
                    )
                    self._persist_task_outcome(
                        task_id=task_id,
                        agent_id=source,
                        event_id=event.get("event_id", ""),
                        event_type=event_type,
                        payload=payload,
                        status=dst,
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
                            commit=False,
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
                policy = self.agent_policies.get(source)
                from orchestrator import agent_control_store

                enabled = agent_control_store.desired_enabled(
                    self.conn, source,
                    policy.get("enabled", True) if policy is not None else True)
                if not enabled:
                    # Desired state is authoritative. Keep the heartbeat event
                    # for audit, but do not register, renew a lease, discover
                    # capabilities, or bind a profile for a disabled Agent.
                    self.conn.execute(
                        "UPDATE agents SET status = 'disabled',"
                        " lease_expires_at = NULL WHERE id = ?;", (source,))
                    self.conn.commit()
                    return "ignored"
                state_store.update_heartbeat(
                    self.conn, source,
                    lease_ttl_seconds=payload.get("lease_ttl_seconds", 90),
                    endpoint=payload.get("endpoint"),
                    skills=payload.get("skills"),
                    commit=False,
                )
                from orchestrator import agent_profile_store

                agent_profile_store.assign_seed_profile(self.conn, source)
            elif event_type == "agent.interaction.requested" and task_id:
                # Some recovery/adapters persist the interaction event
                # independently of task.input_required.  Reconcile any
                # supplied interaction records and always wake an active
                # supervision watch from the authoritative task/interaction
                # rows below.
                if payload.get("interactions"):
                    self._persist_interactions(task_id, source, payload)
            else:
                self.conn.commit()
                return "ignored"
            if task_id and event_type in {
                    "task.input_required", "task.blocked", "task.completed",
                    "task.failed", "task.cancelled",
                    "agent.interaction.requested"}:
                from orchestrator import supervision_store

                supervision_store.sync_task(
                    self.conn, task_id, commit=False)
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

    def _persist_task_outcome(
        self,
        *,
        task_id: str,
        agent_id: str,
        event_id: str,
        event_type: str,
        payload: dict,
        status: TaskStatus,
    ) -> None:
        """Write the worker's final output into its durable conversation."""
        task = state_store.get_task(self.conn, task_id)
        if task is None or not task["collaboration_id"]:
            return
        if event_type == "task.completed":
            text = payload.get("result_text") or payload.get("summary")
        else:
            text = payload.get("error")
        if not isinstance(text, str) or not text.strip():
            return
        text = _bounded_conversation_result(text)
        from orchestrator import collaboration_store

        collaboration = collaboration_store.get_collaboration(
            self.conn, task["collaboration_id"])
        if collaboration is None:
            raise KeyError(
                f"collaboration not found: {task['collaboration_id']}")
        resolved_agent = task["assigned_to"] or agent_id
        collaboration_store.append_message(
            self.conn,
            conversation_id=collaboration["conversation_id"],
            collaboration_id=task["collaboration_id"],
            task_id=task_id,
            agent_id=resolved_agent,
            sender_type="agent",
            sender_id=resolved_agent,
            recipient_type="hermes",
            recipient_id="hermes",
            message_type=(
                "agent.task.result" if event_type == "task.completed"
                else "agent.task.error"
            ),
            content={
                "text": text.strip(),
                "status": status.value,
                "attempt": payload.get("attempt", 1),
            },
            based_on_revision=collaboration["context_revision"],
            idempotency_key=(
                f"task-outcome:{event_id}" if event_id
                else f"task-outcome:{task_id}:{event_type}:"
                     f"{payload.get('attempt', 1)}"
            ),
            commit=False,
        )

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
                commit=False,
            )
        collaboration = collaboration_store.get_collaboration(
            self.conn, task["collaboration_id"])
        if collaboration is None:
            return
        execution_workspace = self._task_execution_workspace(task)
        for raw_item in interactions:
            item = dict(raw_item)
            details = dict(item.get("payload") or {})
            tool_view = details.get("toolView") or {}
            if (agent_id in {"codex", "dsh"}
                    and item.get("kind") == "approval" and tool_view):
                # Recompute before persistence so UI/TaskManager never retain
                # adapter-authored inspectable=true for unverified input.
                if agent_id == "dsh":
                    from adapters.dsh.safety import (
                        normalize_tool_view,
                        tool_view_is_inspectable,
                    )
                else:
                    from adapters.codex.safety import (
                        normalize_tool_view,
                        tool_view_is_inspectable,
                    )

                tool_view = (
                    normalize_tool_view(
                        tool_view, workspace=execution_workspace) or {}
                    if execution_workspace is not None else {
                        **tool_view,
                        "semanticIntent": {
                            "status": "unverified",
                            "operation": "agent.tool.unknown",
                            "impact": "unknown",
                            "targets": {"paths": []},
                            "rollbackPlan": None,
                            "reason": "task execution workspace is invalid",
                        },
                    }
                )
                details["toolView"] = tool_view
                details["inspectable"] = tool_view_is_inspectable(tool_view)
                item["payload"] = details
            saved = collaboration_store.upsert_session_interaction(
                self.conn,
                collaboration_id=task["collaboration_id"],
                task_id=task_id,
                session_binding_id=binding["id"],
                agent_id=agent_id,
                interaction=item,
                commit=False,
            )
            if saved["kind"] != "approval" or saved["action_intent_id"]:
                continue
            semantic = tool_view.get("semanticIntent") or {}
            semantic_targets = semantic.get("targets")
            verified = (
                agent_id in {"codex", "dsh"}
                and semantic.get("status") == "verified"
                and isinstance(semantic.get("operation"), str)
                and bool(semantic.get("operation"))
                and isinstance(semantic_targets, dict)
                and isinstance(semantic_targets.get("paths"), list)
                and bool(semantic_targets.get("paths"))
            )
            targets = (
                dict(semantic_targets) if verified else {
                    "workspace": str(self._workspace_root()),
                    "nativeSessionId": item.get("nativeSessionId"),
                    "callId": details.get("callId"),
                    "paths": [],
                }
            )
            targets.update({
                "nativeSessionId": item.get("nativeSessionId"),
                "callId": details.get("callId"),
            })
            intent = collaboration_store.request_action_intent(
                self.conn,
                collaboration_id=task["collaboration_id"],
                task_id=task_id,
                session_binding_id=binding["id"],
                requested_by_agent_id=agent_id,
                operation=(
                    semantic["operation"] if verified
                    else f"agent.tool.{details.get('toolName') or 'unknown'}"
                ),
                targets=targets,
                purpose=details.get("reason") or "native agent tool request",
                expected_effects={
                    "toolName": details.get("toolName"),
                    "approvalId": details.get("approvalId"),
                    "toolView": tool_view,
                },
                rollback_plan=(semantic.get("rollbackPlan") if verified
                               else None),
                based_on_revision=collaboration["context_revision"],
                commit=False,
            )
            collaboration_store.attach_action_intent(
                self.conn, saved["id"], intent["id"], commit=False)

    @staticmethod
    def _workspace_root() -> Path:
        from common import config as cfg

        return cfg.workspace()

    def _task_execution_workspace(self, task) -> Path | None:
        try:
            context = json.loads(task["plan_context_json"] or "null")
        except (TypeError, ValueError):
            return None
        value = (
            context.get("execution_workspace")
            if isinstance(context, dict) else None
        )
        if value is None:
            return (self._workspace_root() / "tasks" / task["id"]).resolve(
                strict=False)
        try:
            from orchestrator.task_manager import normalize_execution_workspace

            return Path(normalize_execution_workspace(value))
        except (TypeError, ValueError):
            return None

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
        result = writer.apply_resilient(event)
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
