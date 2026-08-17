"""Fake Worker A2A Server — 设计文档 §10。

接口：
  GET  /.well-known/agent-card.json
  GET  /health
  POST /a2a   (JSON-RPC 2.0: message/send, tasks/get)

Phase 1：内存态任务存储，事件 best-effort 发布（NATS 不在则暂存）。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from adapters.common import (
    A2aTask,
    A2aTaskStore,
    EventPublisher,
    FifoExecutor,
)
from adapters.fake import runner
from adapters.fake.card import agent_card
from common.ids import temp_task_id

AGENT_ID = "fake"

store = A2aTaskStore()
publisher = EventPublisher(source=AGENT_ID)
executor = FifoExecutor(max_concurrent=1)


def create_app() -> FastAPI:
    app = FastAPI(title="fake-worker-adapter", version="0.1.0")

    @app.get("/.well-known/agent-card.json")
    async def card(request: Request) -> dict:
        return agent_card(str(request.base_url).rstrip("/"))

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": AGENT_ID, "queued": None}

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

    return app


def _rpc_result(rpc_id, result) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _rpc_error(rpc_id, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
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
        id=temp_task_id(),
        status_state="submitted",
        objective=objective,
        idempotency_key=metadata.get("idempotencyKey"),
    )
    task, created_new = store.create(task)
    if not created_new:
        # 幂等命中（§22.5）：直接返回已有任务
        return _rpc_result(rpc_id, task.to_a2a())

    trace_id = metadata.get("traceId") or f"trace-{uuid.uuid4()}"

    async def execute() -> None:
        store.update_state(task.id, "working")
        await publisher.publish(
            "task.started", task.id,
            {"status_from": "submitted", "status_to": "working", "attempt": 1},
            trace_id=trace_id,
        )
        try:
            artifacts = await runner.run(task)
            task.artifacts = artifacts
            store.update_state(task.id, "completed")
            for a in artifacts:
                await publisher.publish(
                    "artifact.created", task.id,
                    {"name": a["name"], "path": a["path"], "sha256": a["sha256"]},
                    trace_id=trace_id,
                )
            await publisher.publish(
                "task.completed", task.id,
                {
                    "status_from": "working", "status_to": "completed",
                    "attempt": 1, "summary": "fake worker done",
                    "artifacts": [a["name"] for a in artifacts],
                },
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001 — PoC 阶段统一兜底
            store.update_state(task.id, "failed", error=str(exc))
            await publisher.publish(
                "task.failed", task.id,
                {"status_from": "working", "status_to": "failed",
                 "attempt": 1, "error": str(exc)},
                trace_id=trace_id,
            )

    # FIFO 串行执行（§9.1）；message/send 阻塞至完成（PoC 简化）
    await executor.run(execute())
    return _rpc_result(rpc_id, store.get(task.id).to_a2a())


def _tasks_get(body: dict, rpc_id) -> JSONResponse:
    task_id = body.get("params", {}).get("id")
    task = store.get(task_id) if task_id else None
    if task is None:
        return _rpc_error(rpc_id, -32602, f"task not found: {task_id}")
    return _rpc_result(rpc_id, task.to_a2a())


app = create_app()
