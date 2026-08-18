"""Orchestrator A2A Server — 外部总控接入 agentHub 的 A2A 端点。

定位：用户的自建 hermes（总控/规划）经本端点把 agentHub 作为编排执行平面
调用。agentHub 内部 hermes-brain 的规划职责由外部 hermes 接管；审批策略、
产物核验、事件审计等执行侧保障不变。

契约（A2A / JSON-RPC 2.0）：
  GET  /.well-known/agent-card.json   编排者卡片（含 v1.0 supportedInterfaces）
  GET  /health                        探活（免鉴权）
  POST /a2a
    SendMessage   A2A v1.0：text Part 支持 {"text": ..., "mediaType": ...}
                  与 legacy {"kind": "text", ...}；响应包装为 {"task": ...}。
                  peer identity（Bearer）固定路由到映射 worker；
                  legacy identity（X-Agent-Token）走 metadata.agent。
    message/send  legacy：新任务 metadata.agent 必填；响应为 bare Task。
                  metadata.taskId + 自然语言「批准/拒绝」跟进（deprecated，
                  仅留 legacy client；compatibility 路径不走它）。
    tasks/get     params.id → A2A 状态（含 input-required 映射与产物清单）
    tasks/approve params.id → 对待批准（input-required）任务放行并委派
    tasks/reject  params.id → 对待批准任务取消
                  （精确动作；重复/晚到/终态返回稳定错误，不重复委派）

鉴权（/health 外全路径）：
  Authorization: Bearer <token>  LAS_A2A_PEERS 配置的 peer token →
                                 peer identity {peer, worker}（固定路由）
  X-Agent-Token: <token>         LAS_API_TOKEN（回退 LAS_ADAPTER_TOKEN）→
                                 legacy identity（metadata.agent 路由）
  两 header 同时出现且值不一致 → 401。均未配置 = 关闭（仅本地开发）。

运行：python -m orchestrator.a2a_server
（LAS_ORCH_BIND 默认 127.0.0.1，LAS_ORCH_PORT 默认 8310）。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import config as cfg
from common.models import A2A_STATE_MAP, TaskStatus
from hermes.policy import ApprovalPolicy
from orchestrator import state_store
from orchestrator.task_manager import TaskManager

# legacy 自然语言审批（deprecated）：整句精确匹配，不做子串匹配——
# 避免「不批准」因子串含「批准」被误放行。compatibility（SendMessage）
# 路径不使用本表，审批只走 tasks/approve | tasks/reject。
_APPROVE_WORDS = frozenset({"批准", "同意", "放行", "通过", "执行",
                            "approve", "ok", "yes", "go"})
_REJECT_WORDS = frozenset({"拒绝", "取消", "不批", "不批准", "驳回",
                           "reject", "cancel", "no"})

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
                   "以 tasks/approve 放行或 tasks/reject 取消。")
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


def _extract_text(params: dict) -> str:
    """兼容 text extractor：v1.0 member-presence text Part 与 legacy kind:text。"""
    for part in params.get("message", {}).get("parts", []):
        # legacy：{"kind": "text", "text": ...}；v1.0：{"text": ..., "mediaType": ...}
        if part.get("kind") in (None, "text") and isinstance(
                part.get("text"), str):
            return part["text"]
    return ""


def create_app(tm: TaskManager | None = None,
               policy: ApprovalPolicy | None = None) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from common import tracing
        tracing.init_tracing("orchestrator-a2a")
        yield

    app = FastAPI(title="agenthub-orchestrator", version="0.2.0",
                  lifespan=lifespan)
    tm = tm or TaskManager()
    policy = policy or ApprovalPolicy()

    legacy_token = cfg.api_token()
    peers = cfg.a2a_peers()  # token → {peer, worker}
    auth_enabled = bool(legacy_token or peers)

    def _authenticate(request: Request) -> dict | None:
        """解析调用方 identity；未认证返回 None。

        identity: {"peer": <逻辑名>, "worker": <固定 worker 或 None>}
        worker=None 表示 legacy identity，走 metadata.agent 路由。
        """
        if not auth_enabled:
            return {"peer": "dev-local", "worker": None}
        x_tok = request.headers.get("x-agent-token")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        if x_tok and bearer and x_tok != bearer:
            return None  # 双 header 冲突，拒绝
        tok = bearer or x_tok
        if not tok:
            return None
        if tok in peers:
            return {"peer": peers[tok]["peer"], "worker": peers[tok]["worker"]}
        if legacy_token and tok == legacy_token:
            return {"peer": "legacy", "worker": None}
        return None

    @app.middleware("http")
    async def _require_auth(request: Request, call_next):
        if request.url.path != "/health":
            identity = _authenticate(request)
            if identity is None:
                return JSONResponse({"error": "unauthorized"},
                                    status_code=401)
            request.state.identity = identity
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

    @app.get("/.well-known/agent-card.json")
    async def card(request: Request) -> dict:
        base = str(request.base_url).rstrip("/")
        return {
            "name": "agenthub-orchestrator",
            "description": "agentHub 编排执行平面：任务委派、审批门禁、"
                           "产物核验、事件审计。",
            "version": "0.2.0",
            "url": base,
            "supportedInterfaces": [
                # A2A clients select this URL and POST JSON-RPC to it verbatim.
                # Keep the card's top-level URL as the agent base, but advertise
                # the actual JSON-RPC route here.
                {"url": f"{base}/a2a", "protocolBinding": "JSONRPC",
                 "protocolVersion": "1.0"},
            ],
            "capabilities": {"streaming": False},
            "skills": [
                {"id": "orchestrate",
                 "description": "提交任务目标，按 peer identity 固定路由到 "
                                "worker 执行"},
                {"id": "approval-gate",
                 "description": "写操作审批：input-required + "
                                "tasks/approve | tasks/reject 放行"},
            ],
        }

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": "orchestrator"}

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        identity = request.state.identity
        body = await request.json()
        method, rpc_id = body.get("method"), body.get("id")
        params = body.get("params", {})
        if method in ("SendMessage", "message/send"):
            return await _message_send(params, rpc_id, identity,
                                       v1=(method == "SendMessage"))
        if method == "tasks/get":
            return _tasks_get(params, rpc_id)
        if method == "tasks/approve":
            return await _tasks_approval(params, rpc_id, identity,
                                         approve=True)
        if method == "tasks/reject":
            return await _tasks_approval(params, rpc_id, identity,
                                         approve=False)
        return _error(rpc_id, -32601, f"method not found: {method}")

    async def _message_send(params: dict, rpc_id, identity: dict,
                            v1: bool) -> JSONResponse:
        metadata = params.get("message", {}).get("metadata", {}) or {}
        text = _extract_text(params).strip()
        task_id = metadata.get("taskId")
        if task_id:
            if v1:
                # compatibility 路径禁止自然语言审批（防「不批准」误判），
                # 审批只走 tasks/approve | tasks/reject 精确动作。
                return _error(
                    rpc_id, -32602,
                    "SendMessage 不支持自然语言跟进：审批请用 "
                    "tasks/approve / tasks/reject（params.id=任务ID）")
            return await _followup_legacy(task_id, text, rpc_id, identity)
        if not text:
            return _error(rpc_id, -32602, "message has no text part")

        fixed = identity.get("worker")
        claimed = (metadata.get("agent") or "").strip()
        if fixed:
            if claimed and claimed != fixed:
                return _error(
                    rpc_id, -32602,
                    f"peer {identity['peer']} 固定路由到 {fixed}，"
                    f"与 metadata.agent={claimed} 冲突，拒绝")
            agent_id = fixed
        else:
            if not claimed:
                return _error(rpc_id, -32602,
                              "metadata.agent 必填（可用 agent 见 "
                              "agentctl agent list / Web UI）")
            agent_id = claimed
        agent, err = _resolve_agent(tm.conn, agent_id)
        if err:
            return _error(rpc_id, -32602, err)

        tid = tm.create_task(text, project=metadata.get("project"))
        decision = policy.decide(tm.conn, text)
        if decision.action == "ask":
            _record("task.approval_requested", tid,
                    {"agent_id": agent_id, "endpoint": agent["endpoint"],
                     "risk": decision.risk, "reason": decision.reason,
                     "requested_by": identity["peer"]})
        else:
            if decision.action == "granted":
                _record("task.auto_approved", tid,
                        {"grant_id": decision.grant_id,
                         "reason": decision.reason})
            await tm.delegate_task(tid, agent["endpoint"], agent_id)
        row = state_store.get_task(tm.conn, tid)
        task = _to_a2a(tm.conn, row)
        # v1.0 SendMessageResponse：{"task": ...}；legacy 保持 bare Task
        return _result(rpc_id, {"task": task} if v1 else task)

    async def _followup_legacy(task_id: str, text: str, rpc_id,
                               identity: dict) -> JSONResponse:
        """deprecated：legacy message/send 的自然语言审批（整句精确匹配）。"""
        pending = _approval_pending(tm.conn, task_id)
        if pending is None:
            row = state_store.get_task(tm.conn, task_id)
            if row is None:
                return _error(rpc_id, -32602, f"task not found: {task_id}")
            return _error(rpc_id, -32602,
                          f"task {task_id} 不在待批准状态"
                          f"（当前 {row['status']}）")
        word = text.strip().lower()
        if word in _APPROVE_WORDS:
            _record("task.approved", task_id,
                    {"by": identity["peer"], "via": "legacy-nl"})
            await tm.delegate_task(task_id, pending["endpoint"],
                                   pending["agent_id"])
        elif word in _REJECT_WORDS:
            _record("task.rejected", task_id,
                    {"by": identity["peer"], "via": "legacy-nl"})
            state_store.transition_task(tm.conn, task_id,
                                        TaskStatus.CANCELLED)
        else:
            return _error(rpc_id, -32602,
                          "无法解析审批意见：请回复「批准」或「拒绝」，"
                          "或改用 tasks/approve / tasks/reject")
        row = state_store.get_task(tm.conn, task_id)
        return _result(rpc_id, _to_a2a(tm.conn, row))

    async def _tasks_approval(params: dict, rpc_id, identity: dict,
                              approve: bool) -> JSONResponse:
        """精确审批动作（A2A v1.0 compatibility 的唯一审批通道）。"""
        task_id = params.get("id")
        if not task_id:
            return _error(rpc_id, -32602, "params.id 必填（任务 ID）")
        pending = _approval_pending(tm.conn, task_id)
        if pending is None:
            row = state_store.get_task(tm.conn, task_id)
            if row is None:
                return _error(rpc_id, -32602, f"task not found: {task_id}")
            return _error(rpc_id, -32602,
                          f"task {task_id} 不在待批准状态"
                          f"（当前 {row['status']}），忽略重复/晚到操作")
        action = "approve" if approve else "reject"
        if approve:
            _record("task.approved", task_id,
                    {"by": identity["peer"], "via": "tasks/approve"})
            await tm.delegate_task(task_id, pending["endpoint"],
                                   pending["agent_id"])
        else:
            _record("task.rejected", task_id,
                    {"by": identity["peer"], "via": "tasks/reject"})
            state_store.transition_task(tm.conn, task_id,
                                        TaskStatus.CANCELLED)
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
