"""Adapter A2A 服务工厂 — 设计文档 §9 / §10。

所有 Adapter（fake / codex / kimi ...）共享同一套服务行为：
  GET  /.well-known/agent-card.json
  GET  /health
  POST /a2a   (JSON-RPC 2.0: message/send, tasks/get, tasks/cancel,
               extensions/session/pause|resume|interrupt)
  心跳：lifespan 后台循环发布 agent.<id>.heartbeat（§17.4）

差异只在：agent_id、Agent Card、runner 实现、并发上限。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from adapters.common import (
    A2aTask,
    A2aTaskStore,
    EventPublisher,
    FifoExecutor,
)
from adapters.session import (
    RunnerSessionAdapter,
    SessionAdapter,
    SessionCapabilityError,
    SessionMessage,
)
from common import config as cfg
from common.ids import temp_task_id

RunnerFn = Callable[[A2aTask], Awaitable[list[dict]]]
CardFn = Callable[[str], dict]
HealthFn = Callable[[], Awaitable[dict]]


def _card_skills(card_fn: CardFn) -> list[str]:
    """从 Agent Card 提取 skill id 列表（注册用）。"""
    try:
        return [s["id"] for s in (card_fn("").get("skills") or []) if s.get("id")]
    except Exception:
        return []


async def _heartbeat_loop(publisher: EventPublisher, agent_id: str,
                          card_fn: CardFn) -> None:
    # 发现注册（v3 M2）：心跳携带自声明 endpoint 与技能，
    # StateWriter 落库 agents 表，hermes 按租约在线性发现 worker。
    # interval/ttl 每轮动态读 env，便于测试与运维热调。
    endpoint = os.environ.get("LAS_AGENT_ENDPOINT", "").strip()
    skills = _card_skills(card_fn)
    while True:
        payload: dict = {"lease_ttl_seconds": cfg.lease_ttl(),
                         "skills": skills}
        if endpoint:
            payload["endpoint"] = endpoint
        await publisher.publish(f"agent.{agent_id}.heartbeat", None, payload)
        await asyncio.sleep(cfg.heartbeat_interval())


def build_app(
    agent_id: str,
    card_fn: CardFn,
    runner_fn: RunnerFn | None = None,
    max_concurrent: int = 1,
    *,
    session_adapter: SessionAdapter | None = None,
    health_check: HealthFn | None = None,
) -> FastAPI:
    if session_adapter is None:
        if runner_fn is None:
            raise ValueError("runner_fn or session_adapter is required")
        session_adapter = RunnerSessionAdapter(runner_fn)
    store = A2aTaskStore()
    publisher = EventPublisher(source=agent_id)
    executor = FifoExecutor(max_concurrent=max_concurrent)
    adapter_instance_id = f"{agent_id}-{uuid.uuid4()}"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from common import tracing

        tracing.init_tracing(f"adapter-{agent_id}")
        await session_adapter.start()
        hb = asyncio.create_task(_heartbeat_loop(publisher, agent_id, card_fn))
        try:
            yield
        finally:
            hb.cancel()
            with suppress(asyncio.CancelledError):
                await hb
            await session_adapter.close()

    app = FastAPI(title=f"{agent_id}-adapter", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.publisher = publisher
    app.state.session_adapter = session_adapter
    app.state.adapter_instance_id = adapter_instance_id

    # 调用方鉴权（v3 加固）：LAS_ADAPTER_TOKEN 非空时，除 /health 外
    # 所有端点要求 X-Agent-Token 头匹配。直连与经 gateway 均生效
    # （gateway 默认透传该头）。空串 = 关闭（仅本地开发）。
    token = cfg.adapter_token()
    if token:
        @app.middleware("http")
        async def _require_token(request: Request, call_next):
            if request.url.path != "/health":
                if request.headers.get("x-agent-token") != token:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    @app.get("/.well-known/agent-card.json")
    async def card(request: Request) -> dict:
        result = deepcopy(card_fn(str(request.base_url).rstrip("/")))
        result.setdefault("capabilities", {})
        extensions = result["capabilities"].setdefault("extensions", {})
        extensions["agentHubSession"] = session_adapter.capabilities.to_dict()
        return result

    @app.get("/health")
    async def health():
        result = {"status": "ok", "agent": agent_id}
        if health_check is None:
            return result
        try:
            result["dependency"] = await health_check()
            return result
        except Exception as exc:  # noqa: BLE001 - readiness boundary
            return JSONResponse(
                {"status": "unavailable", "agent": agent_id,
                 "error": str(exc)}, status_code=503)

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        body = await request.json()
        method = body.get("method")
        rpc_id = body.get("id")
        if method == "message/send":
            return await _message_send(body, rpc_id)
        if method == "tasks/get":
            return _tasks_get(body, rpc_id)
        if method == "tasks/cancel":
            return await _session_control(body, rpc_id, "cancel")
        if method == "extensions/session/pause":
            return await _session_control(body, rpc_id, "pause")
        if method == "extensions/session/resume":
            return await _session_control(body, rpc_id, "resume")
        if method == "extensions/session/interrupt":
            return await _session_control(body, rpc_id, "interrupt")
        if method == "extensions/session/interactions/respond":
            return await _interaction_respond(body, rpc_id)
        return _rpc_error(rpc_id, -32601, f"method not found: {method}")

    def _rpc_result(rpc_id, result) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def _rpc_error(rpc_id, code: int, message: str) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rpc_id,
             "error": {"code": code, "message": message}}
        )

    def _extract_objective(params: dict) -> str:
        message = params.get("message", {})
        for part in message.get("parts", []):
            if part.get("kind") == "text":
                return part.get("text", "")
        return ""

    async def _message_send(body: dict, rpc_id) -> JSONResponse:
        params = body.get("params", {})
        metadata = params.get("message", {}).get("metadata", {}) or {}
        objective = _extract_objective(params)
        if not objective:
            return _rpc_error(rpc_id, -32602, "message has no text part")

        requested_task_id = metadata.get("taskId")
        existing = store.get(requested_task_id) if requested_task_id else None
        if requested_task_id and existing is not None:
            if (existing.status_state in {
                    "completed", "failed", "canceled", "rejected"}
                    and metadata.get("replaceSession") is True):
                existing = None
            elif existing.status_state in {
                "completed", "failed", "canceled", "rejected"
            }:
                return _rpc_error(
                    rpc_id, -32003,
                    f"task is terminal: {existing.status_state}")
        if requested_task_id and existing is not None:
            if not session_adapter.capabilities.multi_turn:
                return _rpc_error(
                    rpc_id, -32002,
                    "adapter does not support multi-turn sessions")
            if existing.status_state == "paused":
                return _rpc_error(
                    rpc_id, -32006,
                    "session is paused; resume before sending a message")
            if metadata.get("idempotencyKey"):
                duplicate = store.get_by_idempotency_key(
                    metadata["idempotencyKey"])
                if duplicate is not None:
                    return _rpc_result(rpc_id, duplicate.to_a2a())
                store.remember_idempotency_key(
                    metadata["idempotencyKey"], existing.id)
            try:
                requested_revision = int(metadata.get(
                    "contextRevision", existing.context_revision))
            except (TypeError, ValueError):
                return _rpc_error(
                    rpc_id, -32602, "contextRevision must be an integer")
            if requested_revision < existing.context_revision:
                return _rpc_error(
                    rpc_id, -32005,
                    "stale contextRevision: "
                    f"{requested_revision} < {existing.context_revision}")
            existing.objective = objective
            existing.context_revision = requested_revision
            _queue_turn(existing, objective, metadata, first=False)
            return _rpc_result(rpc_id, existing.to_a2a())

        task_id = requested_task_id or temp_task_id()
        session_id = metadata.get("sessionId") or f"S-{uuid.uuid4()}"
        try:
            context_revision = int(metadata.get("contextRevision", 1))
        except (TypeError, ValueError):
            return _rpc_error(
                rpc_id, -32602, "contextRevision must be an integer")
        task = A2aTask(
            id=task_id,  # Hermes 分配的 ID 优先
            status_state="submitted",
            objective=objective,
            idempotency_key=metadata.get("idempotencyKey"),
            context_id=metadata.get("contextId") or task_id,
            session_id=session_id,
            adapter_instance_id=adapter_instance_id,
            native_session_id=metadata.get("nativeSessionId"),
            session_capabilities=session_adapter.capabilities.to_dict(),
            context_revision=context_revision,
        )
        replacing = bool(requested_task_id and store.get(requested_task_id)
                         and metadata.get("replaceSession") is True)
        if replacing:
            task = store.replace(task)
            created_new = True
        else:
            task, created_new = store.create(task)
        if not created_new:
            return _rpc_result(rpc_id, task.to_a2a())  # 幂等命中（§22.5）

        _queue_turn(task, objective, metadata, first=True)
        return _rpc_result(rpc_id, store.get(task.id).to_a2a())

    def _queue_turn(task: A2aTask, objective: str, metadata: dict,
                    *, first: bool) -> None:
        trace_id = metadata.get("traceId") or f"trace-{uuid.uuid4()}"
        message_id = metadata.get("messageId") or f"M-{uuid.uuid4()}"
        store.append_history(task.id, {
            "messageId": message_id,
            "role": "user",
            "parts": [{"kind": "text", "text": objective}],
            "metadata": {
                "contextRevision": task.context_revision,
            },
        })

        async def execute_turn() -> None:
            if task.status_state in {"paused", "canceled"}:
                return
            store.update_state(task.id, "working")
            await publisher.publish(
                "task.started", task.id,
                {"status_from": "submitted", "status_to": "working", "attempt": 1},
                trace_id=trace_id,
            )
            try:
                if first:
                    handle = await session_adapter.start_session(
                        task, session_id=task.session_id or task.id,
                        metadata=metadata)
                    task.native_session_id = handle.native_session_id
                from common import tracing

                tracer = tracing.get_tracer(f"adapter.{agent_id}")
                with tracer.start_as_current_span(
                        "adapter.execute",
                        context=tracing.task_context(trace_id),
                        attributes={"agent.id": agent_id,
                                    "task.id": task.id}):
                    result = await session_adapter.send_message(
                        task.session_id or task.id,
                        SessionMessage(
                            message_id=message_id,
                            role="user",
                            content=objective,
                            based_on_revision=task.context_revision,
                            metadata=metadata,
                        ))
                handle = session_adapter.get_session(
                    task.session_id or task.id)
                if handle is not None:
                    task.native_session_id = handle.native_session_id
                # A concurrent user pause/cancel has higher authority than a
                # late adapter result.  Do not resurrect the session.
                if task.status_state in {"paused", "canceled"}:
                    await publisher.publish(
                        "session.result_discarded", task.id,
                        {"session_id": task.session_id,
                         "reason": task.status_state},
                        trace_id=trace_id,
                    )
                    return
                task.artifacts = result.artifacts
                store.update_state(task.id, result.state)
                for a in result.artifacts:
                    await publisher.publish(
                        "artifact.created", task.id,
                        {"name": a["name"], "path": a["path"],
                         "sha256": a["sha256"]},
                        trace_id=trace_id,
                    )
                if result.state == "completed":
                    await publisher.publish(
                        "task.completed", task.id,
                        {"status_from": "working", "status_to": "completed",
                         "attempt": 1, "summary": f"{agent_id} done",
                         "artifacts": [a["name"] for a in result.artifacts]},
                        trace_id=trace_id,
                    )
                elif result.state == "input-required":
                    pending = [
                        item.to_dict() for item in
                        session_adapter.list_pending_interactions(
                            task.session_id or task.id)
                    ] if session_adapter.capabilities.interactions else []
                    task.pending_interactions = pending
                    await publisher.publish(
                        "task.input_required", task.id,
                        {"status_from": "working",
                         "status_to": "input-required",
                         "interactions": pending,
                         "session_id": task.session_id,
                         "native_session_id": task.native_session_id,
                         "adapter_instance_id": task.adapter_instance_id,
                         "capabilities": task.session_capabilities},
                        trace_id=trace_id,
                    )
            except Exception as exc:  # noqa: BLE001
                if task.status_state in {"paused", "canceled"}:
                    await publisher.publish(
                        "session.result_discarded", task.id,
                        {"session_id": task.session_id,
                         "reason": task.status_state},
                        trace_id=trace_id,
                    )
                    return
                store.update_state(task.id, "failed", error=str(exc))
                await publisher.publish(
                    "task.failed", task.id,
                    {"status_from": "working", "status_to": "failed",
                     "attempt": 1, "error": str(exc)},
                    trace_id=trace_id,
                )

        # A2A 异步化（v3 M1）：send 立即返回，执行在后台。
        # 结果经 NATS 事件 → StateWriter 落库；调用方用 tasks/get 轮询
        # 或订阅事件。长任务不再占用 HTTP 连接（§Evolution v3 §6.3）。
        asyncio.create_task(executor.run(execute_turn()))

    def _tasks_get(body: dict, rpc_id) -> JSONResponse:
        task_id = body.get("params", {}).get("id")
        task = store.get(task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32602, f"task not found: {task_id}")
        handle = session_adapter.get_session(task.session_id or task.id)
        if handle is not None:
            task.native_session_id = handle.native_session_id
        if (session_adapter.capabilities.interactions
                and handle is not None):
            task.pending_interactions = [
                item.to_dict() for item in
                session_adapter.list_pending_interactions(
                    task.session_id or task.id)
            ]
        return _rpc_result(rpc_id, task.to_a2a())

    async def _interaction_respond(body: dict, rpc_id) -> JSONResponse:
        params = body.get("params", {})
        task_id = params.get("id")
        interaction_id = params.get("interactionId")
        response = params.get("response")
        responded_by = params.get("respondedBy")
        task = store.get(task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32602, f"task not found: {task_id}")
        if not session_adapter.capabilities.interactions:
            return _rpc_error(
                rpc_id, -32002,
                "adapter does not support session interactions")
        if task.status_state != "input-required":
            return _rpc_error(
                rpc_id, -32004,
                f"task does not require input: {task.status_state}")
        if not isinstance(interaction_id, str) or not interaction_id:
            return _rpc_error(rpc_id, -32602, "interactionId is required")
        if not isinstance(response, dict):
            return _rpc_error(rpc_id, -32602, "response must be an object")
        if responded_by not in {"user", "hermes"}:
            return _rpc_error(
                rpc_id, -32001,
                "respondedBy must be user or hermes")

        pending = {
            item.interaction_id: item
            for item in session_adapter.list_pending_interactions(
                task.session_id or task.id)
        }
        if interaction_id not in pending:
            return _rpc_error(
                rpc_id, -32602,
                f"pending interaction not found: {interaction_id}")

        try:
            accepted = await session_adapter.respond_interaction(
                task.session_id or task.id,
                interaction_id,
                response,
                responded_by=responded_by,
            )
        except Exception as exc:  # noqa: BLE001 - adapter protocol boundary
            return _rpc_error(rpc_id, -32004, str(exc))

        async def continue_turn() -> None:
            try:
                result = (
                    await session_adapter.continue_after_interaction(
                        task.session_id or task.id)
                    if accepted.state == "working" else accepted
                )
                task.artifacts = result.artifacts
                store.update_state(task.id, result.state)
                for artifact in result.artifacts:
                    await publisher.publish(
                        "artifact.created", task.id,
                        {"name": artifact["name"],
                         "path": artifact["path"],
                         "sha256": artifact["sha256"]},
                    )
                event_type = (
                    "task.completed" if result.state == "completed"
                    else "task.input_required"
                    if result.state == "input-required"
                    else f"task.{result.state}")
                await publisher.publish(
                    event_type, task.id,
                    {"interaction_id": interaction_id,
                     "status_to": result.state,
                     "interactions": [
                         item.to_dict() for item in
                         session_adapter.list_pending_interactions(
                             task.session_id or task.id)
                     ] if result.state == "input-required" else [],
                     "session_id": task.session_id,
                     "native_session_id": task.native_session_id,
                     "adapter_instance_id": task.adapter_instance_id,
                     "capabilities": task.session_capabilities},
                )
            except Exception as exc:  # noqa: BLE001
                store.update_state(task.id, "failed", error=str(exc))
                await publisher.publish(
                    "task.failed", task.id,
                    {"interaction_id": interaction_id, "error": str(exc)},
                )

        store.update_state(task.id, "working")
        await publisher.publish(
            "session.interaction.responded", task.id,
            {"interaction_id": interaction_id,
             "kind": pending[interaction_id].kind,
             "responded_by": responded_by},
        )
        await publisher.publish(
            "task.started", task.id,
            {"status_from": "input-required", "status_to": "working",
             "attempt": 1, "interaction_id": interaction_id},
        )
        asyncio.create_task(executor.run(continue_turn()))
        return _rpc_result(rpc_id, task.to_a2a())

    async def _session_control(body: dict, rpc_id,
                               operation: str) -> JSONResponse:
        task_id = body.get("params", {}).get("id")
        task = store.get(task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32602, f"task not found: {task_id}")
        if task.status_state in {"completed", "failed", "canceled", "rejected"}:
            return _rpc_error(
                rpc_id, -32003, f"task is terminal: {task.status_state}")
        if operation == "resume" and task.status_state != "paused":
            return _rpc_error(
                rpc_id, -32004,
                f"session is not paused: {task.status_state}")
        capability = operation
        if not getattr(session_adapter.capabilities, capability):
            return _rpc_error(
                rpc_id, -32002,
                f"adapter does not support session {operation}")
        try:
            if operation == "resume":
                handle = await session_adapter.resume_session(
                    task.session_id or task.id)
                store.update_state(task.id, "input-required")
            else:
                fn = getattr(session_adapter, operation)
                handle = await fn(task.session_id or task.id)
                state = "canceled" if operation == "cancel" else "paused"
                store.update_state(task.id, state)
        except (KeyError, SessionCapabilityError) as exc:
            return _rpc_error(rpc_id, -32004, str(exc))
        await publisher.publish(
            f"session.{operation}", task.id,
            {"session_id": handle.session_id, "status": handle.status})
        return _rpc_result(rpc_id, task.to_a2a())

    return app
