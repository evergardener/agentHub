"""Adapter A2A 服务工厂 — 设计文档 §9 / §10。

所有 Adapter（fake / codex / kimi ...）共享同一套服务行为：
  GET  /.well-known/agent-card.json
  GET  /health
  POST /a2a   (JSON-RPC 2.0: message/send, tasks/get)
  心跳：lifespan 后台循环发布 agent.<id>.heartbeat（§17.4）

差异只在：agent_id、Agent Card、runner 实现、并发上限。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from adapters.common import (
    A2aTask,
    A2aTaskStore,
    EventPublisher,
    FifoExecutor,
)
from common import config as cfg
from common.ids import temp_task_id

RunnerFn = Callable[[A2aTask], Awaitable[list[dict]]]
CardFn = Callable[[str], dict]


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
    runner_fn: RunnerFn,
    max_concurrent: int = 1,
) -> FastAPI:
    store = A2aTaskStore()
    publisher = EventPublisher(source=agent_id)
    executor = FifoExecutor(max_concurrent=max_concurrent)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from common import tracing

        tracing.init_tracing(f"adapter-{agent_id}")
        hb = asyncio.create_task(_heartbeat_loop(publisher, agent_id, card_fn))
        try:
            yield
        finally:
            hb.cancel()
            with suppress(asyncio.CancelledError):
                await hb

    app = FastAPI(title=f"{agent_id}-adapter", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.publisher = publisher

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
        return card_fn(str(request.base_url).rstrip("/"))

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": agent_id}

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        body = await request.json()
        method = body.get("method")
        rpc_id = body.get("id")
        if method == "message/send":
            return await _message_send(body, rpc_id)
        if method == "tasks/get":
            return _tasks_get(body, rpc_id)
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

        task = A2aTask(
            id=metadata.get("taskId") or temp_task_id(),  # Hermes 分配的 ID 优先
            status_state="submitted",
            objective=objective,
            idempotency_key=metadata.get("idempotencyKey"),
        )
        task, created_new = store.create(task)
        if not created_new:
            return _rpc_result(rpc_id, task.to_a2a())  # 幂等命中（§22.5）

        trace_id = metadata.get("traceId") or f"trace-{uuid.uuid4()}"

        async def execute() -> None:
            store.update_state(task.id, "working")
            await publisher.publish(
                "task.started", task.id,
                {"status_from": "submitted", "status_to": "working", "attempt": 1},
                trace_id=trace_id,
            )
            try:
                from common import tracing

                tracer = tracing.get_tracer(f"adapter.{agent_id}")
                with tracer.start_as_current_span(
                        "adapter.execute",
                        context=tracing.task_context(trace_id),
                        attributes={"agent.id": agent_id,
                                    "task.id": task.id}):
                    artifacts = await runner_fn(task)
                task.artifacts = artifacts
                store.update_state(task.id, "completed")
                for a in artifacts:
                    await publisher.publish(
                        "artifact.created", task.id,
                        {"name": a["name"], "path": a["path"],
                         "sha256": a["sha256"]},
                        trace_id=trace_id,
                    )
                await publisher.publish(
                    "task.completed", task.id,
                    {"status_from": "working", "status_to": "completed",
                     "attempt": 1, "summary": f"{agent_id} done",
                     "artifacts": [a["name"] for a in artifacts]},
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001
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
        asyncio.create_task(executor.run(execute()))
        return _rpc_result(rpc_id, store.get(task.id).to_a2a())

    def _tasks_get(body: dict, rpc_id) -> JSONResponse:
        task_id = body.get("params", {}).get("id")
        task = store.get(task_id) if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32602, f"task not found: {task_id}")
        return _rpc_result(rpc_id, task.to_a2a())

    return app
