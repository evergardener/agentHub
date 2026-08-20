"""Task Manager — 设计文档 §11 task_manager.py / Phase 4。

Hermes 的任务控制面：
  create_task / delegate_task / wait_task / review_result / retry_task / cancel_task
状态写入遵守 §22.3：Hermes 只发生命周期命令（经 state_store）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

from common import config as cfg
from common.ids import idempotency_key as make_idem_key
from common.memory import MemoryService
from common.models import CollaborationPhase, TaskStatus
from orchestrator import state_store
from orchestrator.a2a_client import A2aClient
from state.db import init_db, next_task_id


class TaskManager:
    def __init__(self, db_path: str | Path | None = None,
                 workspace: Path | None = None,
                 memory: "MemoryService | None" = None):
        self.conn: sqlite3.Connection = init_db(db_path)  # None → LAS_DATABASE_URL
        self.workspace = Path(workspace or cfg.workspace())
        # 长期记忆写方仅限 Hermes（§15.3）；None 时静默跳过。
        # best-effort：记忆服务故障不阻塞任务流。
        self.memory = memory

    # ---------- 创建 ----------

    def create_task(self, objective: str, *, project: str | None = None,
                    parent_id: str | None = None,
                    collaboration_id: str | None = None,
                    depends_on: list[str] | None = None,
                    priority: str = "normal",
                    timeout_seconds: int = 1800,
                    max_retries: int = 2,
                    context: dict | str | None = None) -> str:
        task_id = next_task_id(self.conn)
        root_id = parent_id or task_id
        if parent_id:
            parent = state_store.get_task(self.conn, parent_id)
            if parent:
                root_id = parent["root_id"]
        from common import tracing

        tracer = tracing.get_tracer("hermes")
        with tracer.start_as_current_span(
                "task.create",
                context=tracing.task_context(f"trace-{root_id}"),
                attributes={"task.id": task_id, "task.root": root_id,
                            "task.project": project or ""}):
            state_store.create_task(
                self.conn, task_id=task_id, objective=objective,
                created_by="hermes", project=project, parent_id=parent_id,
                root_id=root_id, collaboration_id=collaboration_id,
                priority=priority, depends_on=depends_on,
                plan_context=context if isinstance(context, dict) else None,
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
        context_text = (
            json.dumps(context, ensure_ascii=False, indent=2)
            if isinstance(context, dict) else context
        )
        (tdir / "context.md").write_text(
            context_text or f"# Task {task_id}\n\n{objective}\n",
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
        if row["status"] == TaskStatus.QUEUED.value:
            needs_assignment = True
        elif row["status"] not in {
            TaskStatus.ASSIGNED.value,
            TaskStatus.WORKING.value,
            TaskStatus.BLOCKED.value,
        }:
            raise state_store.IllegalTransition(
                task_id, row["status"], TaskStatus.ASSIGNED.value)
        else:
            needs_assignment = False

        from common import tracing

        tracer = tracing.get_tracer("hermes")
        with tracer.start_as_current_span(
                "task.delegate",
                context=tracing.task_context(f"trace-{row['root_id']}"),
                attributes={"task.id": task_id, "agent.id": agent_id,
                            "agent.endpoint": endpoint}):
            pass  # span 仅记录委派时点；A2A 调用在后台进行

        # 异步 A2A：send 立即返回，60s 足够；结果经事件/轮询对齐（v3 M1）
        client = A2aClient.for_agent(agent_id, endpoint, timeout=60)

        collaboration_id = row["collaboration_id"]
        binding = None
        recovery_plan = "new"
        adapter_session_id = f"S-{uuid.uuid4()}"
        native_session_id = None
        context_revision = None
        turn_sequence = 1
        if collaboration_id:
            from orchestrator import collaboration_store

            collaboration = collaboration_store.get_collaboration(
                self.conn, collaboration_id)
            if collaboration is None:
                raise KeyError(
                    f"collaboration not found: {collaboration_id}")
            context_revision = collaboration["context_revision"]
            binding = collaboration_store.get_current_agent_session(
                self.conn, task_id, agent_id)
            recovery_plan = collaboration_store.session_recovery_plan(binding)
            if binding is not None:
                turn_sequence = binding["last_message_seq"] + 1
            if binding is not None and recovery_plan == "native_resume":
                adapter_session_id = (
                    binding["adapter_session_id"] or adapter_session_id)
                native_session_id = binding["native_session_id"]
            elif binding is not None:
                # A non-native or incomplete binding is replaced with a fresh
                # adapter session and a durable context snapshot.
                adapter_session_id = f"S-{uuid.uuid4()}"
            if recovery_plan == "blocked":
                raise RuntimeError(
                    f"safe session recovery unavailable for "
                    f"{task_id}/{agent_id}")

        if needs_assignment:
            state_store.transition_task(self.conn, task_id, TaskStatus.ASSIGNED)
        from state.db import now_iso

        self.conn.execute(
            "UPDATE tasks SET assigned_to = ?, updated_at = ?"
            " WHERE id = ?;", (agent_id, now_iso(), task_id),
        )
        self.conn.commit()

        def _session_metadata(task: dict) -> tuple[str | None, dict, str | None]:
            meta = (task.get("metadata") or {}).get("agentHub") or {}
            return (meta.get("nativeSessionId"),
                    meta.get("capabilities") or {},
                    meta.get("adapterInstanceId"))

        def _save_binding(task: dict, *, recovery_state: str):
            if not collaboration_id:
                return None
            from orchestrator import collaboration_store

            discovered_native, capabilities, instance_id = \
                _session_metadata(task)
            native = discovered_native or native_session_id
            if native and capabilities.get("native_resume") is True:
                resume_capability = "native"
            elif capabilities.get("durable_session") is False:
                resume_capability = "snapshot"
            else:
                resume_capability = "unknown"
            return collaboration_store.upsert_agent_session(
                self.conn, collaboration_id=collaboration_id,
                task_id=task_id, agent_id=agent_id,
                adapter_session_id=(
                    ((task.get("metadata") or {}).get("agentHub") or {})
                    .get("sessionId") or adapter_session_id),
                native_session_id=native, capabilities=capabilities,
                adapter_instance_id=instance_id,
                resume_capability=resume_capability,
                recovery_state=recovery_state,
                context_snapshot={
                    "objective": row["objective"],
                    "project": row["project"],
                    "context_revision": context_revision,
                    "task_plan": json.loads(
                        row["plan_context_json"] or "null"),
                })

        async def _call() -> None:
            dispatched = False
            try:
                delivery_sequence = (
                    turn_sequence if collaboration_id else attempt)
                plan_context = json.loads(row["plan_context_json"] or "null")
                instruction = row["objective"]
                if plan_context:
                    instruction += (
                        "\n\n[AgentHub structured Task Plan contract]\n"
                        + json.dumps(plan_context, ensure_ascii=False, indent=2)
                        + "\nAny modifying operation must be requested as a structured "
                        "ActionIntent before execution. Ask Hermes when requirements, "
                        "risks, or acceptance criteria are ambiguous."
                    )
                response = await client.send_message(
                    instruction,
                    idempotency_key=make_idem_key(
                        task_id, delivery_sequence),
                    trace_id=f"trace-{row['root_id']}",
                    task_id=task_id,
                    session_id=adapter_session_id,
                    native_session_id=native_session_id,
                    context_revision=context_revision,
                    replace_session=binding is not None,
                    metadata={"recoveryMode": recovery_plan,
                              "taskPlan": plan_context},
                )
                dispatched = True
                saved_binding = _save_binding(
                    response,
                    recovery_state=(
                        "resumed" if recovery_plan == "native_resume"
                        else "replaced" if binding is not None else "none"))
                if saved_binding is not None:
                    from orchestrator import collaboration_store

                    collaboration_store.advance_agent_session(
                        self.conn, saved_binding["id"],
                        message_seq=turn_sequence,
                        context_revision=context_revision or 1)

                # Native IDs often appear after message/send returns. Poll the
                # short-lived task view until the ID or a terminal state is
                # visible; lifecycle status still comes from NATS/StateWriter.
                for _ in range(150):
                    meta = ((response.get("metadata") or {})
                            .get("agentHub") or {})
                    if meta.get("nativeSessionId"):
                        break
                    if response.get("status", {}).get("state") in {
                        "completed", "failed", "canceled", "rejected"
                    }:
                        break
                    await asyncio.sleep(0.2)
                    response = await client.get_task(task_id)
                _save_binding(
                    response,
                    recovery_state=(
                        "resumed" if recovery_plan == "native_resume"
                        else "replaced" if binding is not None else "none"))
            except Exception as exc:
                if collaboration_id:
                    try:
                        from orchestrator import collaboration_store

                        current = collaboration_store.get_current_agent_session(
                            self.conn, task_id, agent_id)
                        if current is not None:
                            collaboration_store.update_agent_session_status(
                                self.conn, current["id"],
                                status=(current["status"] if dispatched
                                        else "error"),
                                recovery_state=("tracking_failed" if dispatched
                                                else "failed"),
                                error=str(exc))
                    except Exception:
                        pass
                # A2A 调用本身失败（Adapter 不可达等）：走重试流程
                if not dispatched:
                    try:
                        state_store.transition_task(
                            self.conn, task_id, TaskStatus.FAILED,
                            error_message="a2a call failed",
                        )
                    except Exception:
                        pass

        return asyncio.create_task(_call())

    async def control_agent_session(self, task_id: str, *, agent_id: str,
                                    endpoint: str, operation: str,
                                    requested_by: str = "hermes") -> dict:
        """Apply a remote session control, then persist its authoritative state."""
        if requested_by not in {"hermes", "user"}:
            raise PermissionError("only hermes or user may control a session")
        if operation not in {"pause", "resume", "interrupt", "cancel"}:
            raise ValueError(f"unsupported session control: {operation}")
        row = state_store.get_task(self.conn, task_id)
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        from orchestrator import collaboration_store

        binding = collaboration_store.get_current_agent_session(
            self.conn, task_id, agent_id)
        if binding is None:
            raise KeyError(
                f"agent session not found: {task_id}/{agent_id}")
        client = A2aClient.for_agent(agent_id, endpoint, timeout=60)
        result = await client.control_session(task_id, operation)
        status = {
            "pause": "paused", "interrupt": "paused",
            "resume": "active", "cancel": "canceled",
        }[operation]
        collaboration_store.update_agent_session_status(
            self.conn, binding["id"], status=status,
            recovery_state=operation)
        if row["collaboration_id"]:
            phase = {
                "pause": CollaborationPhase.PAUSED,
                "interrupt": CollaborationPhase.PAUSED,
                "resume": CollaborationPhase.EXECUTING,
                "cancel": CollaborationPhase.CANCELLED,
            }[operation]
            collaboration_store.set_phase(
                self.conn, row["collaboration_id"], phase)
        state_store.record_event(self.conn, {
            "event_id": f"session-control-{uuid.uuid4()}",
            "event_type": f"agent.session.{operation}",
            "task_id": task_id,
            "source": requested_by,
            "payload": {
                "agent_id": agent_id, "binding_id": binding["id"],
                "operation": operation,
            },
        })
        if operation == "cancel":
            self.cancel_task(task_id)
        return result

    async def intervene_agent_session(
        self,
        task_id: str,
        *,
        mode: str,
        content,
        agent_id: str | None = None,
        endpoint: str | None = None,
        user_id: str = "user",
        idempotency_key: str | None = None,
    ) -> dict:
        """Persist user authority first, then apply the matching native action."""
        from orchestrator import collaboration_store

        task = state_store.get_task(self.conn, task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if not task["collaboration_id"]:
            raise ValueError("task is not attached to a collaboration")
        agent_id = agent_id or task["assigned_to"]
        if not agent_id:
            raise ValueError("task has no assigned agent")
        binding = collaboration_store.get_current_agent_session(
            self.conn, task_id, agent_id)
        if binding is None:
            raise KeyError(f"agent session not found: {task_id}/{agent_id}")
        existing = None
        if idempotency_key:
            existing = self.conn.execute(
                "SELECT * FROM conversation_messages"
                " WHERE idempotency_key = ?;", (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (existing["message_type"] != f"user.{mode}"
                        or existing["task_id"] != task_id
                        or existing["agent_id"] != agent_id):
                    raise ValueError(
                        "idempotency key belongs to another intervention")
                if existing["delivery_status"] == "delivered":
                    return {
                        "task_id": task_id, "agent_id": agent_id,
                        "mode": mode, "message_id": existing["id"],
                        "context_revision": existing["based_on_revision"],
                        "duplicate": True,
                    }
        message = existing or collaboration_store.record_user_intervention(
            self.conn, collaboration_id=task["collaboration_id"],
            user_id=user_id, mode=mode, content=content,
            task_id=task_id, agent_id=agent_id,
            idempotency_key=idempotency_key)
        revision = message["based_on_revision"]
        result: dict | None = None
        if mode == "steer":
            capabilities = json.loads(binding["capabilities_json"] or "{}")
            if capabilities.get("steer") is not True:
                raise ValueError(
                    f"agent {agent_id} does not support same-turn steer; "
                    "interrupt it and send a new turn")
            text = (content.get("text") if isinstance(content, dict)
                    else content)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("steer content must contain non-empty text")
            if endpoint is None:
                agent = self.conn.execute(
                    "SELECT endpoint FROM agents WHERE id = ?;",
                    (agent_id,),
                ).fetchone()
                endpoint = agent["endpoint"] if agent is not None else None
            if not endpoint:
                raise RuntimeError(f"agent endpoint unavailable: {agent_id}")
            client = A2aClient.for_agent(agent_id, endpoint, timeout=60)
            result = await client.steer_session(
                task_id, text, context_revision=revision,
                message_id=message["id"],
            )
            collaboration_store.advance_agent_session(
                self.conn, binding["id"], message_seq=message["sequence"],
                context_revision=revision)
            collaboration_store.set_phase(
                self.conn, task["collaboration_id"],
                CollaborationPhase.EXECUTING, controller="user")
            state_store.record_event(self.conn, {
                "event_id": f"session-steer-{uuid.uuid4()}",
                "event_type": "agent.session.steer.requested",
                "task_id": task_id,
                "source": user_id,
                "payload": {
                    "agent_id": agent_id, "binding_id": binding["id"],
                    "message_id": message["id"],
                    "context_revision": revision,
                },
            })
        elif mode in {"pause", "interrupt", "cancel"}:
            if endpoint is None:
                agent = self.conn.execute(
                    "SELECT endpoint FROM agents WHERE id = ?;",
                    (agent_id,),
                ).fetchone()
                endpoint = agent["endpoint"] if agent is not None else None
            if not endpoint:
                raise RuntimeError(f"agent endpoint unavailable: {agent_id}")
            result = await self.control_agent_session(
                task_id, agent_id=agent_id, endpoint=endpoint,
                operation=mode, requested_by="user")
        elif mode == "return_to_hermes":
            collaboration_store.set_phase(
                self.conn, task["collaboration_id"],
                CollaborationPhase.NEEDS_REPLAN, controller="hermes")
        elif mode not in {"comment", "takeover"}:
            raise ValueError(f"unsupported intervention mode: {mode}")
        self.conn.execute(
            "UPDATE conversation_messages SET delivery_status = 'delivered'"
            " WHERE id = ?;", (message["id"],))
        self.conn.commit()
        return {
            "task_id": task_id, "agent_id": agent_id, "mode": mode,
            "message_id": message["id"], "context_revision": revision,
            "native_result": result,
        }

    async def respond_agent_interaction(
        self,
        interaction_id: str,
        *,
        response: dict,
        requested_by: str,
        endpoint: str | None = None,
    ) -> dict:
        """Authorize and deliver a response to the same native agent turn."""
        if requested_by not in {"user", "hermes"}:
            raise PermissionError(
                "only user or hermes may respond to agent interactions")
        from orchestrator import collaboration_store

        interaction = collaboration_store.get_session_interaction(
            self.conn, interaction_id)
        if interaction is None:
            raise KeyError(f"interaction not found: {interaction_id}")
        if interaction["status"] != "pending":
            raise ValueError(
                f"interaction already {interaction['status']}")
        task = state_store.get_task(self.conn, interaction["task_id"])
        if task is None:
            raise KeyError(f"task not found: {interaction['task_id']}")
        agent_id = interaction["agent_id"]
        if endpoint is None:
            agent = self.conn.execute(
                "SELECT endpoint FROM agents WHERE id = ?;",
                (agent_id,),
            ).fetchone()
            endpoint = agent["endpoint"] if agent is not None else None
        if not endpoint:
            raise RuntimeError(f"agent endpoint unavailable: {agent_id}")

        outbound = dict(response)
        if interaction["kind"] == "approval":
            outcome = outbound.get("outcome")
            if outcome not in {"allowed-once", "rejected"}:
                raise ValueError(
                    "approval outcome must be allowed-once or rejected")
            try:
                interaction_payload = json.loads(
                    interaction["payload_json"] or "{}")
            except (TypeError, ValueError):
                interaction_payload = {}
            if (outcome == "allowed-once"
                    and interaction_payload.get("inspectable") is not True):
                raise PermissionError(
                    "approval details are incomplete; only rejection is safe")
            action_intent_id = interaction["action_intent_id"]
            if not action_intent_id:
                raise RuntimeError(
                    "approval interaction has no ActionIntent")
            intent = self.conn.execute(
                "SELECT * FROM action_intents WHERE id = ?;",
                (action_intent_id,),
            ).fetchone()
            if intent is None:
                raise KeyError(
                    f"action intent not found: {action_intent_id}")
            if intent["status"] in {"awaiting_hermes", "awaiting_user"}:
                intent = collaboration_store.decide_action_intent(
                    self.conn, action_intent_id,
                    approved=outcome == "allowed-once",
                    decided_by=requested_by,
                    note="native agent interaction response",
                )
            expected = "approved" if outcome == "allowed-once" else "rejected"
            if intent["status"] != expected:
                raise PermissionError(
                    f"ActionIntent is {intent['status']}; expected {expected}")
            if outcome == "allowed-once":
                from common.action_receipt import sign_action_receipt

                binding = self.conn.execute(
                    "SELECT native_session_id FROM agent_session_bindings"
                    " WHERE id = ?;",
                    (interaction["session_binding_id"],),
                ).fetchone()
                outbound["authorization"] = sign_action_receipt({
                    "actionIntentId": action_intent_id,
                    "status": intent["status"],
                    "decidedBy": intent["decided_by"],
                    "decidedAt": intent["decided_at"],
                    "basedOnRevision": intent["based_on_revision"],
                    "taskId": interaction["task_id"],
                    "interactionId": interaction["adapter_interaction_id"],
                    "nativeRequestId": interaction["native_request_id"],
                    "nativeSessionId": (
                        binding["native_session_id"] if binding else None),
                    "contextRevision": intent["based_on_revision"],
                })
        elif interaction["kind"] != "question":
            raise ValueError(
                f"unsupported interaction kind: {interaction['kind']}")

        collaboration_store.resolve_session_interaction(
            self.conn, interaction_id, status="responding",
            resolved_by=requested_by, response=outbound)
        client = A2aClient.for_agent(agent_id, endpoint, timeout=60)
        try:
            result = await client.respond_interaction(
                interaction["task_id"],
                interaction["adapter_interaction_id"],
                outbound,
                responded_by=requested_by,
            )
        except Exception as exc:
            collaboration_store.resolve_session_interaction(
                self.conn, interaction_id, status="failed",
                resolved_by=requested_by, response=outbound,
                error=str(exc))
            raise
        collaboration_store.resolve_session_interaction(
            self.conn, interaction_id, status="resolved",
            resolved_by=requested_by, response=outbound)
        return result

    # ---------- 等待（event-driven，NATS 不可用时降级 DB 轮询） ----------

    async def wait_task(self, task_id: str, timeout: float = 600.0,
                        nats_url: str | None = None) -> str:
        """等待任务到达 completed/failed/cancelled。返回最终状态。"""
        terminal = {"completed", "failed", "cancelled", "accepted"}

        async def _db_status() -> str | None:
            row = state_store.get_task(self.conn, task_id)
            return row["status"] if row else None

        url = nats_url or cfg.nats_url()
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
        row0 = state_store.get_task(self.conn, task_id)
        root_id = row0["root_id"] if row0 else task_id
        from common import tracing

        tracer = tracing.get_tracer("hermes")
        with tracer.start_as_current_span(
                "task.review",
                context=tracing.task_context(f"trace-{root_id}"),
                attributes={"task.id": task_id, "review.approved": approved,
                            "review.reviewer": reviewer}):
            review = {"reviewer": reviewer,
                      "verdict": "approved" if approved else "rejected",
                      "notes": notes}
            state_store.transition_task(self.conn, task_id, TaskStatus.REVIEWED,
                                        review=review)
            # 复审历史落事件流（Web UI 可追溯 veto/驳回原因；
            # tasks.review_json 只留最新一次）
            state_store.record_event(self.conn, {
                "event_id": f"review-{task_id}-{uuid.uuid4().hex[:8]}",
                "event_type": "task.reviewed", "task_id": task_id,
                "payload": review,
            })
            if approved:
                state_store.transition_task(self.conn, task_id,
                                            TaskStatus.ACCEPTED)
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
        self._reject_generic_native_interaction_decision(task_id)
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
        self._reject_generic_native_interaction_decision(task_id)
        self.cancel_task(task_id)
        return "cancelled"

    def _reject_generic_native_interaction_decision(self, task_id: str) -> None:
        pending = self.conn.execute(
            "SELECT id FROM agent_session_interactions WHERE task_id = ?"
            " AND status IN ('pending', 'responding') LIMIT 1;",
            (task_id,),
        ).fetchone()
        if pending is not None:
            raise ValueError(
                "task has a native agent interaction; decide it through "
                f"/api/interactions/{pending['id']}/respond")

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
