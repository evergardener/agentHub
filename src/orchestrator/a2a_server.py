"""Orchestrator A2A Server — 外部总控接入 agentHub 的 A2A 端点。

定位：用户的自建 hermes（总控/规划）经本端点把 agentHub 作为编排执行平面
调用。agentHub 内部 hermes-brain 的规划职责由外部 hermes 接管；审批策略、
产物核验、事件审计等执行侧保障不变。

契约（A2A / JSON-RPC 2.0）：
  GET  /.well-known/agent-card.json   编排者卡片
  GET  /health                        探活（免鉴权）
  POST /a2a
    message/send  新任务：text=目标，metadata.agent=目标 worker（必填——
                  派给谁由外部 hermes 规划）；可选 metadata.project。
                  审批策略判定 ask 时不委派，任务呈现 input-required。
                  跟进：metadata.taskId + text「批准/拒绝」放行或取消。
    tasks/get     params.id → A2A 状态（含 input-required 映射与产物清单）

鉴权：X-Agent-Token 头（LAS_API_TOKEN，回退 LAS_ADAPTER_TOKEN；均空=关闭，
仅本地开发）。运行：python -m orchestrator.a2a_server
（LAS_ORCH_BIND 默认 127.0.0.1，LAS_ORCH_PORT 默认 8310）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import config as cfg
from common.models import A2A_STATE_MAP, TaskStatus
from hermes.policy import ApprovalPolicy
from orchestrator import state_store
from orchestrator.task_manager import TaskManager

_APPROVE_WORDS = ("批准", "同意", "放行", "通过", "可以", "执行",
                  "approve", "ok", "yes", "go")
_REJECT_WORDS = ("拒绝", "取消", "不批", "驳回", "reject", "cancel", "no")

_APPROVAL_EVENTS = ("task.approval_requested", "task.approved",
                    "task.auto_approved")


def _now(conn) -> str:
    from state.db import now_iso
    return now_iso()


def _resolve_agent(conn, agent_id: str) -> tuple[dict | None, str | None]:
    """从注册表解析在线 worker；返回 (info, error)。"""
    row = conn.execute(
        "SELECT id, endpoint, lease_expires_at FROM agents WHERE id = ?;",
        (agent_id,)).fetchone()
    if row is None:
        online = [r["id"] for r in conn.execute(
            "SELECT id FROM agents WHERE lease_expires_at > ?;",
            (_now(conn),)).fetchall()]
        return None, f"unknown agent: {agent_id}（在线: {online or '无'}）"
    if not (row["lease_expires_at"] and row["lease_expires_at"] > _now(conn)):
        return None, f"agent offline: {agent_id}（心跳租约已过期）"
    if not row["endpoint"]:
        return None, f"agent {agent_id} 无可用 endpoint（未完成注册）"
    return {"id": row["id"], "endpoint": row["endpoint"]}, None


def _approval_pending(conn, task_id: str) -> dict | None:
    """任务处于等待批准状态时返回审批上下文（含拟委派 agent），否则 None。"""
    row = state_store.get_task(conn, task_id)
    if row is None or row["status"] not in (TaskStatus.CREATED.value,
                                            TaskStatus.QUEUED.value):
        return None
    ev = conn.execute(
        "SELECT event_type, payload_json FROM events WHERE task_id = ?"
        " AND event_type IN (?,?,?) ORDER BY seq DESC LIMIT 1;",
        (task_id, *_APPROVAL_EVENTS)).fetchone()
    if ev and ev["event_type"] == "task.approval_requested":
        return json.loads(ev["payload_json"])
    return None


def _to_a2a(conn, row) -> dict:
    """内部任务行 → A2A Task；created/queued + 待批准 → input-required。"""
    task_id = row["id"]
    pending = _approval_pending(conn, task_id)
    if pending:
        state = "input-required"
        message = (f"写操作需批准（risk={pending.get('risk')}）："
                   f"{pending.get('reason')}。拟委派 {pending.get('agent_id')}。"
                   "回复「批准」放行，「拒绝」取消。")
    else:
        state = A2A_STATE_MAP[TaskStatus(row["status"])]
        message = row["error_message"] or row["result_summary"] or ""
    artifacts = [
        {"name": a["name"], "type": a["type"], "path": a["path"]}
        for a in state_store.list_artifacts(conn, task_id)
    ]
    return {
        "id": task_id,
        "status": {"state": state, "timestamp": row["updated_at"],
                   "message": message},
        "artifacts": artifacts,
        "metadata": {"assigned_to": row["assigned_to"],
                     "internal_status": row["status"]},
    }


def create_app(tm: TaskManager | None = None,
               policy: ApprovalPolicy | None = None) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from common import tracing
        tracing.init_tracing("orchestrator-a2a")
        yield

    app = FastAPI(title="agenthub-orchestrator", version="0.1.0",
                  lifespan=lifespan)
    tm = tm or TaskManager()
    policy = policy or ApprovalPolicy()

    token = cfg.api_token()
    if token:
        @app.middleware("http")
        async def _require_token(request: Request, call_next):
            if request.url.path != "/health":
                if request.headers.get("x-agent-token") != token:
                    return JSONResponse({"error": "unauthorized"},
                                        status_code=401)
            return await call_next(request)

    def _result(rpc_id: Any, result: dict) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def _error(rpc_id: Any, code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "error": {"code": code, "message": message}})

    def _record(event_type: str, task_id: str, payload: dict) -> None:
        state_store.record_event(tm.conn, {
            "event_id": f"{event_type}-{task_id}-{uuid.uuid4().hex[:8]}",
            "event_type": event_type, "task_id": task_id,
            "payload": payload,
        })

    def _extract_text(params: dict) -> str:
        for part in params.get("message", {}).get("parts", []):
            if part.get("kind") == "text":
                return part.get("text", "")
        return ""

    @app.get("/.well-known/agent-card.json")
    async def card(request: Request) -> dict:
        base = str(request.base_url).rstrip("/")
        return {
            "name": "agenthub-orchestrator",
            "description": "agentHub 编排执行平面：任务委派、审批门禁、"
                           "产物核验、事件审计。",
            "url": base,
            "capabilities": {"streaming": False},
            "skills": [
                {"id": "orchestrate",
                 "description": "提交任务目标并指定 worker 执行"},
                {"id": "approval-gate",
                 "description": "写操作审批：input-required + 批准/拒绝放行"},
            ],
        }

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": "orchestrator"}

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        body = await request.json()
        method, rpc_id = body.get("method"), body.get("id")
        params = body.get("params", {})
        if method == "message/send":
            return await _message_send(params, rpc_id)
        if method == "tasks/get":
            return _tasks_get(params, rpc_id)
        return _error(rpc_id, -32601, f"method not found: {method}")

    async def _message_send(params: dict, rpc_id) -> JSONResponse:
        metadata = params.get("message", {}).get("metadata", {}) or {}
        text = _extract_text(params).strip()
        task_id = metadata.get("taskId")
        if task_id:
            return await _followup(task_id, text, rpc_id)
        if not text:
            return _error(rpc_id, -32602, "message has no text part")

        agent_id = (metadata.get("agent") or "").strip()
        if not agent_id:
            return _error(rpc_id, -32602,
                          "metadata.agent 必填（可用 agent 见 "
                          "agentctl agent list / Web UI）")
        agent, err = _resolve_agent(tm.conn, agent_id)
        if err:
            return _error(rpc_id, -32602, err)

        tid = tm.create_task(text, project=metadata.get("project"))
        decision = policy.decide(tm.conn, text)
        if decision.action == "ask":
            _record("task.approval_requested", tid,
                    {"agent_id": agent_id, "endpoint": agent["endpoint"],
                     "risk": decision.risk, "reason": decision.reason})
        else:
            if decision.action == "granted":
                _record("task.auto_approved", tid,
                        {"grant_id": decision.grant_id,
                         "reason": decision.reason})
            await tm.delegate_task(tid, agent["endpoint"], agent_id)
        row = state_store.get_task(tm.conn, tid)
        return _result(rpc_id, _to_a2a(tm.conn, row))

    async def _followup(task_id: str, text: str, rpc_id) -> JSONResponse:
        pending = _approval_pending(tm.conn, task_id)
        if pending is None:
            row = state_store.get_task(tm.conn, task_id)
            if row is None:
                return _error(rpc_id, -32602, f"task not found: {task_id}")
            return _error(rpc_id, -32602,
                          f"task {task_id} 不在待批准状态"
                          f"（当前 {row['status']}）")
        lowered = text.lower()
        if any(w in lowered for w in _APPROVE_WORDS):
            _record("task.approved", task_id, {"by": "external-hermes"})
            await tm.delegate_task(task_id, pending["endpoint"],
                                   pending["agent_id"])
        elif any(w in lowered for w in _REJECT_WORDS):
            _record("task.rejected", task_id, {"by": "external-hermes"})
            state_store.transition_task(tm.conn, task_id, TaskStatus.CANCELLED)
        else:
            return _error(rpc_id, -32602,
                          "无法解析审批意见：请回复「批准」或「拒绝」")
        row = state_store.get_task(tm.conn, task_id)
        return _result(rpc_id, _to_a2a(tm.conn, row))

    def _tasks_get(params: dict, rpc_id) -> JSONResponse:
        task_id = params.get("id")
        row = state_store.get_task(tm.conn, task_id) if task_id else None
        if row is None:
            return _error(rpc_id, -32602, f"task not found: {task_id}")
        return _result(rpc_id, _to_a2a(tm.conn, row))

    return app


app = None  # 惰性：测试用 create_app(自建 tm)；服务进程走 main()


def main() -> None:
    import uvicorn

    global app
    app = create_app()
    uvicorn.run(app,
                host=os.environ.get("LAS_ORCH_BIND", "127.0.0.1"),
                port=int(os.environ.get("LAS_ORCH_PORT", "8310")))


if __name__ == "__main__":
    main()
