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
                  peer identity（Bearer）通过 agentHub 控制包调用 Registry；
                  legacy identity（X-Agent-Token）仍走 metadata.agent。
    message/send  legacy：新任务 metadata.agent 必填；响应为 bare Task。
                  metadata.taskId + 自然语言「批准/拒绝」跟进（deprecated，
                  仅留 legacy client；compatibility 路径不走它）。
    tasks/get     params.id → A2A 状态（含 input-required 映射与产物清单）
    tasks/approve params.id → 对待批准（input-required）任务放行并委派
    tasks/reject  params.id → 对待批准任务取消
    tasks/accept  params.id → 对待验收任务执行正式验收通过
    tasks/request-rework params.id, feedback → 对待验收任务提报反馈返工
                  （精确动作；重复/晚到/终态返回稳定错误，不重复委派）
    interactions/get params.interaction_id → pending native interaction detail
    interactions/respond params.interaction_id, outcome → one-time response
                  supervision/register|pull|ack|stop → qishuo 原会话的持久异步监督；
                  仅返回任务/事件标识，不返回 worker 内容

鉴权（/health 外全路径）：
  Authorization: Bearer <token>  LAS_A2A_PEERS 配置的 caller token →
                                 peer identity（worker 由 Registry 动态发现）
  X-Agent-Token: <token>         LAS_API_TOKEN（回退 LAS_ADAPTER_TOKEN）→
                                 legacy identity（metadata.agent 路由）
  两 header 同时出现且值不一致 → 401。均未配置只允许 loopback 开发模式；
  LAS_ORCH_REQUIRE_AUTH=true 或绑定非 loopback 时启动即失败。

运行：python -m orchestrator.a2a_server
（LAS_ORCH_BIND 默认 127.0.0.1，LAS_ORCH_PORT 默认 8310）。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from common import config as cfg
from common.models import A2A_STATE_MAP, TaskStatus
from hermes.policy import ApprovalPolicy, normalize_access_mode
from orchestrator import state_store
from orchestrator.task_manager import (
    TaskManager,
    normalize_execution_workspace,
    require_structured_workspace,
)

# legacy 自然语言审批（deprecated）：整句精确匹配，不做子串匹配——
# 避免「不批准」因子串含「批准」被误放行。compatibility（SendMessage）
# 路径不使用本表，审批只走 tasks/approve | tasks/reject。
_APPROVE_WORDS = frozenset({"批准", "同意", "放行", "通过", "执行",
                            "approve", "ok", "yes", "go"})
_REJECT_WORDS = frozenset({"拒绝", "取消", "不批", "不批准", "驳回",
                           "reject", "cancel", "no"})

_APPROVAL_EVENTS = ("task.approval_requested", "task.approved",
                    "task.auto_approved")


def validate_orchestrator_security(host: str) -> None:
    """Reject production/non-loopback startup without strong credentials."""
    legacy_token = cfg.api_token()
    peers = cfg.a2a_peers()
    auth_enabled = bool(legacy_token or peers)
    if cfg.orchestrator_require_auth() and not auth_enabled:
        raise RuntimeError(
            "LAS_ORCH_REQUIRE_AUTH=true 但 LAS_API_TOKEN/LAS_A2A_PEERS 为空")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback and not auth_enabled:
        raise RuntimeError("Orchestrator 绑定非 loopback 地址时必须配置认证")
    if cfg.orchestrator_require_auth() or not loopback:
        configured = ([legacy_token] if legacy_token else []) + list(peers)
        if any(len(token) < 16 for token in configured):
            raise RuntimeError("Orchestrator 生产认证 token 至少 16 个字符")


def _now(conn) -> str:
    from state.db import now_iso
    return now_iso()


def _resolve_agent(conn, agent_id: str) -> tuple[dict | None, str | None]:
    """从注册表解析在线 worker；返回 (info, error)。"""
    from orchestrator import agent_control_store

    if not agent_control_store.desired_enabled(conn, agent_id, True):
        return None, (f"agent disabled: {agent_id}（不参与探测或委派；"
                      "需询问用户是否启用后重新探测）")
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


def _text_message(text: str, context_id: str | None = None) -> dict:
    message = {
        "role": "agent",
        "parts": [{"text": text, "mediaType": "text/plain"}],
        "messageId": uuid.uuid4().hex,
    }
    if context_id:
        message["contextId"] = context_id
    return message


def _hub_command(text: str) -> tuple[dict | None, str | None]:
    """解析 qishuo 经原生 a2a_call 发送的严格 agentHub v1 控制包。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, "消息必须是 agentHub v1 JSON 控制包"
    if not isinstance(value, dict) or value.get("agenthub") != "v1":
        return None, "消息缺少 agenthub=v1"
    action = value.get("action")
    if action not in {
            "agents/list", "tasks/create", "tasks/get",
            "tasks/approve", "tasks/reject", "tasks/accept",
            "tasks/request-rework", "interactions/respond",
            "interactions/get",
            "supervision/register", "supervision/pull",
            "supervision/ack", "supervision/stop"}:
        return None, f"未知 agentHub action: {action}"
    return value, None


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


def _infer_input_required_kind(conn, row, *, pending: dict | None) -> str:
    """从状态与上下文推断 input-required 的输入类型，便于 Hermes 分流。"""
    status = TaskStatus(row["status"])
    if pending is not None:
        return "delegation"
    if status in {TaskStatus.AWAITING_ACCEPTANCE, TaskStatus.COMPLETED}:
        return "acceptance"
    if status == TaskStatus.REVIEWED:
        return "review"
    if status == TaskStatus.REWORK_PENDING:
        return "rework"
    if status == TaskStatus.BLOCKED:
        return "blocked"
    return "input-required"


def _canonical_internal_status(value: str) -> tuple[str, str | None]:
    """Expose historical ``completed`` rows as the acceptance gate they are.

    Worker completion used to be persisted as ``completed`` before explicit
    acceptance was introduced.  Leaking that legacy name makes clients infer
    terminal success even though the only legal next step is user acceptance.
    Keep the stored value available for audit, but publish one canonical
    lifecycle vocabulary to A2A consumers.
    """
    if value == TaskStatus.COMPLETED.value:
        return TaskStatus.AWAITING_ACCEPTANCE.value, value
    return value, None


def _input_required_actions(
        input_required_kind: str | None,
        pending_interactions: list[dict]
        ) -> tuple[str, str, list[str], str, list[str]]:
    """Describe the next gate without overloading A2A's state enum.

    A2A 1.0 has no ``awaiting-acceptance`` TaskState.  Keep the wire state at
    ``input-required`` for protocol compatibility, but expose a stable,
    machine-readable detail and the exact actions that can advance that gate.
    In particular, an acceptance gate must never advertise the approval
    action used by the pre-dispatch delegation gate.
    """
    if input_required_kind == "delegation":
        return (
            "awaiting_delegation_approval",
            "hermes_approval",
            ["tasks/approve", "tasks/reject"],
            "hermes",
            [],
        )
    if input_required_kind in {"acceptance", "review"}:
        return (
            "awaiting_acceptance",
            "explicit_user_acceptance",
            [],
            "user",
            ["accept", "request-rework"],
        )
    if input_required_kind == "blocked":
        hermes_interaction = any(
            item.get("inspectable") is True
            and item.get("action_intent_status") == "awaiting_hermes"
            for item in pending_interactions
        )
        if hermes_interaction:
            return (
                "awaiting_hermes_interaction",
                "interaction_response",
                ["interactions/respond"],
                "hermes",
                [],
            )
        return (
            "awaiting_user_interaction",
            "user_interaction",
            [],
            "user",
            ["approve", "reject"],
        )
    return (input_required_kind or "input-required", "input", [],
            "user", [])


def _message_interaction_summary(
        pending_interactions: list[dict]) -> tuple[list[dict], bool]:
    """Build a small renderer-safe hint; metadata remains authoritative."""
    if not pending_interactions:
        return [], False
    item = pending_interactions[0]
    summary = [{
        "interaction_id": item.get("interaction_id"),
        "inspectable": item.get("inspectable") is True,
        "operation": str(item.get("operation") or "")[:128] or None,
        "risk": item.get("risk"),
        "policy_route": str(item.get("policy_route") or "")[:32] or None,
        "policy_reason": str(item.get("policy_reason") or "")[:160],
        "action_intent_status": str(
            item.get("action_intent_status") or "")[:64] or None,
        "awaiting": str(item.get("awaiting") or "")[:64] or None,
        "command": str(item.get("command") or "")[:160] or None,
        "args": [str(arg)[:96] for arg in (item.get("args") or [])[:4]],
        "cwd": str(item.get("cwd") or "")[:160] or None,
        "workspace": str(item.get("workspace") or "")[:160] or None,
        "rollback_plan": str(item.get("rollback_plan") or "")[:160]
        or None,
        "allowed_responses": [
            str(value)[:64]
            for value in (item.get("allowed_responses") or [])[:8]
        ],
    }]
    return summary, len(pending_interactions) > 1


def _to_a2a(conn, row, *, context_id: str | None = None) -> dict:
    """内部任务行 → A2A Task；created/queued + 待批准 → input-required。"""
    task_id = row["id"]
    internal_status, legacy_internal_status = _canonical_internal_status(
        row["status"])
    pending = _approval_pending(conn, task_id)
    input_required_kind = None
    from orchestrator import collaboration_store

    pending_interactions = collaboration_store.pending_interaction_views(
        conn, task_id)
    raw_detail = row["error_message"] or row["result_summary"] or ""
    detail = str(raw_detail)[:1000]
    detail_truncated = len(str(raw_detail)) > len(detail)
    if pending_interactions:
        # Interaction persistence and lifecycle events are intentionally
        # independent.  Surface the concrete gate immediately even if the
        # task.input_required event has not moved the task to BLOCKED yet.
        state = "input-required"
        message = f"task_id={task_id}; status={state}"
        input_required_kind = "blocked"
    elif pending:
        state = "input-required"
        message = (f"task_id={task_id}; 写操作需批准"
                   f"（risk={pending.get('risk')}）："
                   f"{pending.get('reason')}。拟委派 {pending.get('agent_id')}。")
        input_required_kind = "delegation"
    else:
        state = A2A_STATE_MAP[TaskStatus(row["status"])]
        message = f"task_id={task_id}; status={state}"
        if state == "input-required":
            input_required_kind = _infer_input_required_kind(
                conn, row, pending=None)
    if state == "input-required":
        (state_detail, required_action, available_actions, required_actor,
         ui_actions) = (
            _input_required_actions(input_required_kind, pending_interactions))
        message += (
            f"; input_required_kind={input_required_kind}"
            f"; internal_status={internal_status}"
            f"; state_detail={state_detail}"
            f"; required_action={required_action}"
            f"; required_actor={required_actor}"
            f"; available_actions={','.join(available_actions) or 'none'}")
        if input_required_kind == "acceptance":
            message += "；等待用户验收，不是委派审批或运行时审批"
        elif input_required_kind == "delegation":
            message += "；等待 Hermes 处理委派审批"
        elif input_required_kind == "blocked":
            message += "；等待原生交互处理"
    else:
        state_detail, required_action, available_actions = state, "none", []
        required_actor, ui_actions = "none", []
    if detail:
        message += f"; {detail}"
        if detail_truncated:
            message += "; status_detail_truncated=true"
    artifacts = [
        {"name": a["name"], "type": a["type"], "path": a["path"],
         **({"sha256": a["sha256"]} if a["sha256"] else {})}
        for a in state_store.list_artifacts(conn, task_id)
    ]
    metadata = {"assigned_to": row["assigned_to"],
                "internal_status": internal_status,
                "state_detail": state_detail,
                "required_action": required_action,
                "required_actor": required_actor,
                "available_actions": available_actions,
                "ui_actions": ui_actions,
                "status_detail_truncated": detail_truncated}
    if legacy_internal_status is not None:
        metadata["legacy_internal_status"] = legacy_internal_status
    try:
        plan_context = json.loads(row["plan_context_json"] or "null")
    except (TypeError, ValueError):
        plan_context = None
    if isinstance(plan_context, dict):
        for source_key, target_key in (
                ("execution_workspace", "execution_workspace"),
                ("display_title", "display_title"),
                ("objective_summary", "objective_summary"),
                ("runtime_config", "runtime_config")):
            if plan_context.get(source_key):
                metadata[target_key] = plan_context[source_key]
        if plan_context.get("access_mode"):
            metadata["access_mode"] = plan_context["access_mode"]
    if state == "input-required" and input_required_kind is not None:
        metadata["input_required_kind"] = input_required_kind
        # Keep the field stable for every input-required task.  Delegation and
        # acceptance gates have no native interaction records, but callers can
        # safely consume one shape without guessing which gate is active.
        metadata["pending_interactions"] = pending_interactions
    if pending_interactions:
        # Some Hermes renderers surface only status.message and hide metadata.
        # Include a compact, bounded *summary* there while retaining the full
        # structured records above.  Never copy tool payloads/worker output
        # into the wakeup envelope or this summary.
        summary, summary_truncated = _message_interaction_summary(
            pending_interactions)
        compact = json.dumps(summary, ensure_ascii=False,
                             separators=(",", ":"))
        metadata["pending_interactions_summary"] = {
            "total": len(pending_interactions),
            "included": len(summary),
            "truncated": summary_truncated,
            "authoritative_field": "metadata.pending_interactions",
            "detail_action": "interactions/get",
        }
        message += (
            f"; pending_interactions_total={len(pending_interactions)}"
            f"; pending_interactions_truncated="
            f"{str(summary_truncated).lower()}"
            f"; pending_interactions={compact}"
            "; authoritative_details=metadata.pending_interactions"
            "; detail_action=interactions/get"
        )
    return {
        "id": task_id,
        "status": {"state": state, "timestamp": row["updated_at"],
                   "message": _text_message(message, context_id)},
        "artifacts": artifacts,
        "metadata": metadata,
        **({"contextId": context_id} if context_id else {}),
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
    peers = cfg.a2a_peers()  # token → {peer}
    gateway_token = cfg.gateway_bearer_token()
    auth_enabled = bool(legacy_token or peers)
    if cfg.orchestrator_require_auth() and not auth_enabled:
        raise RuntimeError(
            "LAS_ORCH_REQUIRE_AUTH=true 但 LAS_API_TOKEN/LAS_A2A_PEERS 为空")
    if cfg.orchestrator_require_auth():
        configured = ([legacy_token] if legacy_token else []) + list(peers)
        if any(len(token) < 16 for token in configured):
            raise RuntimeError("Orchestrator 生产认证 token 至少 16 个字符")

    def _authenticate(request: Request) -> dict | None:
        """解析调用方 identity；未认证返回 None。

        identity: {"peer": <逻辑名>, "kind": "hub"|"legacy"|"gateway"}
        """
        if not auth_enabled:
            return {"peer": "dev-local", "kind": "legacy"}
        x_tok = request.headers.get("x-agent-token")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        # /worker-proxy carries two deliberately different credentials:
        # Bearer authenticates agentgateway to this control plane, while
        # X-Agent-Token is an opaque downstream adapter credential that must
        # survive the proxy hop.  Do not compare those two identities.
        if request.url.path.startswith("/worker-proxy/"):
            if (bearer and gateway_token
                    and hmac.compare_digest(bearer, gateway_token)):
                return {"peer": "agentgateway", "kind": "gateway"}
            return None
        if x_tok and bearer and not hmac.compare_digest(x_tok, bearer):
            return None  # 双 header 冲突，拒绝
        tok = bearer or x_tok
        if not tok:
            return None
        for candidate, meta in peers.items():
            if hmac.compare_digest(tok, candidate):
                return {"peer": meta["peer"], "kind": "hub"}
        if gateway_token and hmac.compare_digest(tok, gateway_token):
            return {"peer": "agentgateway", "kind": "gateway"}
        if legacy_token and hmac.compare_digest(tok, legacy_token):
            return {"peer": "legacy", "kind": "legacy"}
        return None

    @app.middleware("http")
    async def _require_auth(request: Request, call_next):
        if request.url.path not in {"/health", "/ready"}:
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
        base = os.environ.get("LAS_ORCH_PUBLIC_URL", "").rstrip("/") \
            or str(request.base_url).rstrip("/")
        return {
            "name": "agenthub-orchestrator",
            "description": "agentHub 编排执行平面：任务委派、审批门禁、"
                           "持久监督、产物核验、事件审计。",
            "version": "0.3.0",
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
                 "description": "查询 Registry，选择已启用且在线的 "
                                "Agent 并委派"},
                {"id": "registry-discovery",
                 "description": "agents/list 返回实时 Agent/Profile 发现视图"},
                {"id": "approval-gate",
                 "description": "写操作审批：input-required + "
                                "tasks/approve | tasks/reject；原生交互由 "
                                "interactions/respond 按 ActionIntent 路由"},
                {"id": "durable-supervision",
                 "description": "peer/context 隔离的持久 watch 与租约 outbox；"
                                "为原 Hermes 会话提供审批、失败和验收唤醒"},
            ],
        }

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "agent": "orchestrator"}

    @app.get("/ready")
    async def ready():
        try:
            tm.conn.execute("SELECT 1;").fetchone()
            return {"status": "ready", "agent": "orchestrator"}
        except Exception:
            return JSONResponse({"status": "not-ready"}, status_code=503)

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
            return _tasks_get(
                params, rpc_id, identity=identity,
                context_id=(params.get("contextId") or
                            params.get("context_id")))
        if method == "tasks/approve":
            return await _tasks_approval(params, rpc_id, identity,
                                         approve=True,
                                         context_id=(params.get("contextId") or
                                                     params.get("context_id")))
        if method == "tasks/reject":
            return await _tasks_approval(params, rpc_id, identity,
                                         approve=False,
                                         context_id=(params.get("contextId") or
                                                     params.get("context_id")))
        if method == "tasks/accept":
            return await _tasks_acceptance(
                params, rpc_id, identity, approve=True,
                context_id=(params.get("contextId") or
                            params.get("context_id")))
        if method == "tasks/request-rework":
            return await _tasks_acceptance(
                params, rpc_id, identity, approve=False,
                context_id=(params.get("contextId") or
                            params.get("context_id")))
        if method == "interactions/respond":
            return await _interaction_response(
                params, rpc_id, identity,
                context_id=(params.get("contextId") or
                            params.get("context_id")), wrapped=False)
        if method == "interactions/get":
            return _interaction_get(
                params, rpc_id, identity,
                context_id=(params.get("contextId") or
                            params.get("context_id")), wrapped=False)
        return _error(rpc_id, -32601, f"method not found: {method}")

    @app.api_route("/worker-proxy/{agent_id}/{path:path}",
                   methods=["GET", "POST"])
    async def worker_proxy(agent_id: str, path: str, request: Request):
        """gateway 后的通用 worker 路由；目标每次从 Registry 解析。"""
        identity = request.state.identity
        if identity.get("kind") != "gateway":
            return JSONResponse({"error": "gateway identity required"},
                                status_code=403)
        agent, err = _resolve_agent(tm.conn, agent_id)
        if err:
            return JSONResponse({"error": err}, status_code=503)
        target = f"{agent['endpoint'].rstrip('/')}/{path.lstrip('/')}"
        headers = {
            name: value for name, value in request.headers.items()
            if name.lower() in {
                "content-type", "accept", "a2a-version", "x-agent-token",
                "idempotency-key", "traceparent", "tracestate"}
        }
        try:
            async with httpx.AsyncClient(
                    timeout=900, follow_redirects=False,
                    trust_env=False) as client:
                response = await client.request(
                    request.method, target, params=request.query_params,
                    content=await request.body(), headers=headers)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": f"worker proxy unavailable: {type(exc).__name__}"},
                status_code=502)
        return Response(
            content=response.content, status_code=response.status_code,
            media_type=response.headers.get("content-type"))

    async def _message_send(params: dict, rpc_id, identity: dict,
                            v1: bool) -> JSONResponse:
        metadata = params.get("message", {}).get("metadata", {}) or {}
        text = _extract_text(params).strip()
        context_id = params.get("message", {}).get("contextId")
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

        if identity.get("kind") == "hub":
            command, err = _hub_command(text)
            if err:
                return _error(rpc_id, -32602, err)
            action = command["action"]
            if action == "agents/list":
                from hermes.tools import HermesTools
                from orchestrator import agent_profile_store

                agents = HermesTools(tm, policy)._resolve_agents()
                public = []
                for agent_id, info in sorted(agents.items()):
                    profile = (
                        agent_profile_store.profile_policy(
                            tm.conn, info["profile_id"])
                        if info.get("profile_id") else None
                    )
                    public.append({
                        "id": agent_id,
                        "enabled": info.get("enabled") is not False,
                        "online": info.get("online"),
                        "skills": info.get("skills") or [],
                        "profile_id": info.get("profile_id"),
                        "runtime_policy": ({
                            "model": profile.get("model"),
                            "allowed_models": (
                                profile.get("allowed_models") or []),
                            "reasoning_effort": profile.get(
                                "reasoning_effort"),
                            "allowed_reasoning_efforts": profile.get(
                                "allowed_reasoning_efforts") or [],
                        } if profile else None),
                    })
                message = _text_message(json.dumps(
                    {"agents": public}, ensure_ascii=False), context_id)
                return _result(rpc_id, {"message": message})
            if action == "tasks/get":
                return _tasks_get(
                    {"id": command.get("task_id")}, rpc_id,
                    identity=identity, context_id=context_id, wrapped=True)
            if action in {"tasks/approve", "tasks/reject"}:
                return await _tasks_approval(
                    {"id": command.get("task_id")}, rpc_id, identity,
                    approve=action == "tasks/approve", context_id=context_id,
                    wrapped=True)
            if action in {"tasks/accept", "tasks/request-rework"}:
                return await _tasks_acceptance(
                    {
                        "id": command.get("task_id"),
                        "feedback": command.get("feedback"),
                        "notes": command.get("notes"),
                    },
                    rpc_id,
                    identity,
                    approve=(action == "tasks/accept"),
                    context_id=context_id,
                    wrapped=True,
                )
            if action == "interactions/respond":
                return await _interaction_response(
                    {
                        "interaction_id": command.get("interaction_id"),
                        "outcome": command.get("outcome"),
                        "note": command.get("note"),
                    },
                    rpc_id,
                    identity,
                    context_id=context_id,
                    wrapped=True,
                )
            if action == "interactions/get":
                return _interaction_get(
                    {"interaction_id": command.get("interaction_id")},
                    rpc_id,
                    identity,
                    context_id=context_id,
                    wrapped=True,
                )
            if action.startswith("supervision/"):
                from orchestrator import supervision_store

                try:
                    if action == "supervision/register":
                        if not context_id:
                            raise ValueError(
                                "supervision/register requires contextId")
                        watch = supervision_store.register_watch(
                            tm.conn,
                            peer=identity["peer"],
                            context_id=context_id,
                            task_id=command.get("task_id"),
                        )
                        payload = {
                            "status": watch["status"],
                            "watch_id": watch["watch_id"],
                            "task_id": watch["task_id"],
                            "context_id": watch["context_id"],
                        }
                    elif action == "supervision/pull":
                        payload = {"notifications":
                            supervision_store.pull_notifications(
                                tm.conn,
                                peer=identity["peer"],
                                watch_ids=command.get("watch_ids") or [],
                                limit=command.get("limit") or 20,
                            )}
                    elif action == "supervision/ack":
                        payload = supervision_store.acknowledge_notification(
                            tm.conn,
                            peer=identity["peer"],
                            notification_id=command.get("notification_id"),
                        )
                    else:
                        payload = supervision_store.stop_watch(
                            tm.conn,
                            peer=identity["peer"],
                            task_id=command.get("task_id"),
                        )
                except PermissionError as exc:
                    return _error(rpc_id, -32003, str(exc))
                except (KeyError, TypeError, ValueError) as exc:
                    return _error(rpc_id, -32602, str(exc))
                message = _text_message(
                    json.dumps(payload, ensure_ascii=False), context_id)
                return _result(rpc_id, {"message": message})
            agent_id = str(command.get("agent") or "").strip()
            text = str(command.get("objective") or "").strip()
            if not agent_id or not text:
                return _error(
                    rpc_id, -32602,
                    "tasks/create 需要非空 agent 和 objective；"
                    "先用 agents/list 发现")
            metadata = {
                "project": command.get("project"),
                "workspace": command.get("workspace"),
                "title": command.get("title"),
                "summary": command.get("summary"),
                "model": command.get("model"),
                "reasoning_effort": command.get("reasoning_effort"),
                "access_mode": command.get("access_mode"),
            }
        else:
            claimed = (metadata.get("agent") or "").strip()
            if not claimed:
                return _error(rpc_id, -32602,
                              "metadata.agent 必填（可用 agent 见 "
                              "agentctl agent list / Web UI）")
            agent_id = claimed
        agent, err = _resolve_agent(tm.conn, agent_id)
        if err:
            return _error(rpc_id, -32602, err)
        try:
            access_mode = normalize_access_mode(metadata.get("access_mode"))
        except (TypeError, ValueError) as exc:
            return _error(rpc_id, -32602, str(exc))
        from orchestrator.runtime_config import (
            normalize_runtime_config,
            resolve_runtime_config,
        )
        try:
            requested_runtime = normalize_runtime_config(
                metadata.get("model"), metadata.get("reasoning_effort"))
            runtime_config = resolve_runtime_config(
                tm.conn, agent_id=agent_id, requested=requested_runtime)
        except (PermissionError, TypeError, ValueError) as exc:
            return _error(rpc_id, -32602, str(exc))
        try:
            execution_workspace = (
                normalize_execution_workspace(metadata["workspace"])
                if metadata.get("workspace") is not None else None
            )
        except (TypeError, ValueError) as exc:
            return _error(rpc_id, -32602, str(exc))
        try:
            require_structured_workspace(text, execution_workspace)
        except ValueError as exc:
            return _error(rpc_id, -32602, str(exc))
        display_title = metadata.get("title")
        objective_summary = metadata.get("summary")
        for key, value, maximum in (
                ("title", display_title, 100),
                ("summary", objective_summary, 500)):
            if value is not None and (
                    not isinstance(value, str) or not value.strip()
                    or len(value.strip()) > maximum):
                return _error(
                    rpc_id, -32602,
                    f"{key} 必须是 1-{maximum} 字符的非空字符串")
        display_title = display_title.strip() if display_title else None
        objective_summary = (
            objective_summary.strip() if objective_summary else None)

        collaboration_id = None
        a2a_context = None
        if identity.get("kind") == "hub":
            from orchestrator import collaboration_store

            context_id = context_id or f"ctx-agenthub-{uuid.uuid4().hex}"
            try:
                a2a_context = collaboration_store.ensure_a2a_collaboration(
                    tm.conn,
                    peer=identity["peer"],
                    context_id=context_id,
                    objective=text,
                    project=metadata.get("project"),
                )
            except (ValueError, RuntimeError) as exc:
                return _error(rpc_id, -32602, str(exc))
            collaboration_id = a2a_context["collaboration_id"]

        tid = tm.create_task(
            text,
            project=metadata.get("project"),
            collaboration_id=collaboration_id,
            context={
                **({"display_title": display_title}
                   if display_title else {}),
                **({"objective_summary": objective_summary}
                   if objective_summary else {}),
                **({"runtime_config": runtime_config}
                   if runtime_config else {}),
                **({"access_mode": access_mode}
                   if access_mode else {}),
            } or None,
            execution_workspace=execution_workspace,
        )
        if a2a_context is not None:
            from orchestrator import collaboration_store

            collaboration_store.append_message(
                tm.conn,
                conversation_id=a2a_context["conversation_id"],
                collaboration_id=collaboration_id,
                task_id=tid,
                agent_id=agent_id,
                sender_type="hermes",
                sender_id=identity["peer"],
                recipient_type="agent",
                recipient_id=agent_id,
                message_type="a2a.task.request",
                content={
                    "text": text,
                    "agent": agent_id,
                    "contextId": context_id,
                    **({"workspace": execution_workspace}
                       if execution_workspace else {}),
                    **({"title": display_title} if display_title else {}),
                    **({"summary": objective_summary}
                       if objective_summary else {}),
                    **({"runtime_config": runtime_config}
                       if runtime_config else {}),
                    **({"access_mode": access_mode}
                       if access_mode else {}),
                },
                based_on_revision=1,
                idempotency_key=f"a2a-task-request:{tid}",
            )
            _record(
                "task.a2a_context.bound",
                tid,
                {
                    "peer": identity["peer"],
                    "context_id": context_id,
                    "conversation_id": a2a_context["conversation_id"],
                    "collaboration_id": collaboration_id,
                },
            )
        if runtime_config:
            _record(
                "task.runtime_config.selected",
                tid,
                {"agent_id": agent_id, "runtime_config": runtime_config},
            )
        if access_mode:
            _record(
                "task.access_mode.selected",
                tid,
                {"agent_id": agent_id, "access_mode": access_mode,
                 "source": "caller_semantic_declaration",
                 "authority": "initial_dispatch_only"},
            )
        decision = policy.decide(
            tm.conn, text, access_mode=access_mode,
            require_structured_read=True)
        _record(
            "task.intent.classified",
            tid,
            {"risk": decision.risk, "decision": decision.action,
             "reason": decision.reason,
             "source": ("structured_access_mode"
                        if access_mode else "legacy_keyword_compatibility"),
             "runtime_authority": False,
             "runtime_gate": "structured_action_intent"},
        )
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
        task = _to_a2a(tm.conn, row, context_id=context_id)
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
            await tm.approve_task_request(
                task_id, decided_by=identity["peer"], via="legacy-nl")
        elif word in _REJECT_WORDS:
            await tm.reject_task_request(
                task_id, decided_by=identity["peer"], via="legacy-nl")
        else:
            return _error(rpc_id, -32602,
                          "无法解析审批意见：请回复「批准」或「拒绝」，"
                          "或改用 tasks/approve / tasks/reject")
        row = state_store.get_task(tm.conn, task_id)
        return _result(rpc_id, _to_a2a(tm.conn, row))

    async def _tasks_approval(params: dict, rpc_id, identity: dict,
                              approve: bool, *, context_id: str | None = None,
                              wrapped: bool = False) -> JSONResponse:
        """精确审批动作（A2A v1.0 compatibility 的唯一审批通道）。"""
        task_id = params.get("id")
        if not task_id:
            return _error(rpc_id, -32602, "params.id 必填（任务 ID）")
        if identity.get("kind") == "hub":
            if not isinstance(context_id, str) or not context_id.strip():
                return _error(rpc_id, -32602,
                              "tasks/approve 或 tasks/reject 必须携带 contextId")
            from orchestrator import collaboration_store
            try:
                collaboration_store.require_a2a_task(
                    tm.conn, task_id=task_id, peer=identity["peer"],
                    context_id=context_id)
            except PermissionError as exc:
                return _error(rpc_id, -32003, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                return _error(rpc_id, -32602, str(exc))
        pending = _approval_pending(tm.conn, task_id)
        if pending is None:
            row = state_store.get_task(tm.conn, task_id)
            if row is None:
                return _error(rpc_id, -32602, f"task not found: {task_id}")
            return _error(rpc_id, -32602,
                          f"task {task_id} 不在待批准状态"
                          f"（当前 {row['status']}），忽略重复/晚到操作")
        if approve:
            await tm.approve_task_request(
                task_id, decided_by=identity["peer"], via="tasks/approve")
        else:
            await tm.reject_task_request(
                task_id, decided_by=identity["peer"], via="tasks/reject")
        row = state_store.get_task(tm.conn, task_id)
        task = _to_a2a(tm.conn, row, context_id=context_id)
        return _result(rpc_id, {"task": task} if wrapped else task)

    async def _tasks_acceptance(params: dict, rpc_id, identity: dict,
                               approve: bool, *, context_id: str | None = None,
                               wrapped: bool = False) -> JSONResponse:
        """验收/返工动作：tasks/accept + tasks/request-rework。"""
        # An authenticated Hermes peer proves service identity, not that a
        # human explicitly accepted this particular result.  Until the
        # protocol carries a user-signed acceptance receipt, formal acceptance
        # and rework decisions are WebUI-only.
        return _error(
            rpc_id, -32003,
            "formal acceptance/rework requires explicit user authority in WebUI",
        )

    async def _interaction_response(
            params: dict, rpc_id, identity: dict, *,
            context_id: str | None = None,
            wrapped: bool = False) -> JSONResponse:
        """Let an authenticated Hermes peer decide Hermes-routed intents."""
        if identity.get("kind") != "hub":
            return _error(
                rpc_id, -32003,
                "原生 Agent 交互只能由已认证 Hermes peer 或用户 WebUI 处理")
        interaction_id = params.get("interaction_id") or params.get("id")
        outcome = params.get("outcome")
        if not interaction_id:
            return _error(rpc_id, -32602, "interaction_id 必填")
        if outcome not in {"allowed-once", "rejected"}:
            return _error(
                rpc_id, -32602,
                "outcome 必须是 allowed-once 或 rejected")
        note = params.get("note") or ""
        if not isinstance(note, str) or len(note) > 2000:
            return _error(
                rpc_id, -32602, "note 必须是至多 2000 字符的字符串")
        if not isinstance(context_id, str) or not context_id.strip():
            return _error(rpc_id, -32602,
                          "interactions/respond 必须携带 contextId")
        from orchestrator import collaboration_store
        try:
            collaboration_store.require_a2a_interaction(
                tm.conn,
                interaction_id=interaction_id.strip(),
                peer=identity["peer"],
                context_id=context_id,
            )
        except PermissionError as exc:
            return _error(rpc_id, -32003, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return _error(rpc_id, -32602, str(exc))
        try:
            native_result = await tm.respond_agent_interaction(
                interaction_id.strip(),
                response={"outcome": outcome, "note": note.strip()},
                requested_by="hermes",
            )
        except PermissionError as exc:
            return _error(rpc_id, -32003, str(exc))
        except (KeyError, ValueError, RuntimeError) as exc:
            return _error(rpc_id, -32602, str(exc))
        result = {
            "status": "responded",
            "interaction_id": interaction_id,
            "outcome": outcome,
            "native_result": native_result,
        }
        if wrapped:
            return _result(rpc_id, {"message": _text_message(
                json.dumps(result, ensure_ascii=False), context_id)})
        return _result(rpc_id, result)

    def _interaction_get(params: dict, rpc_id, identity: dict, *,
                         context_id: str | None = None,
                         wrapped: bool = False) -> JSONResponse:
        """Return one bounded native interaction after A2A ownership checks."""
        if identity.get("kind") != "hub":
            return _error(
                rpc_id, -32003,
                "原生 Agent 交互只能由已认证 Hermes peer 或用户 WebUI 读取")
        interaction_id = params.get("interaction_id") or params.get("id")
        if (not isinstance(interaction_id, str)
                or not interaction_id.strip() or len(interaction_id) > 128):
            return _error(rpc_id, -32602, "interaction_id 必填")
        if not isinstance(context_id, str) or not context_id.strip():
            return _error(rpc_id, -32602,
                          "interactions/get 必须携带 contextId")

        from orchestrator import collaboration_store

        try:
            collaboration_store.require_a2a_interaction(
                tm.conn,
                interaction_id=interaction_id.strip(),
                peer=identity["peer"],
                context_id=context_id,
            )
        except PermissionError as exc:
            return _error(rpc_id, -32003, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return _error(rpc_id, -32602, str(exc))
        detail = collaboration_store.get_session_interaction_view(
            tm.conn, interaction_id.strip())
        if detail is None:
            return _error(rpc_id, -32602,
                          f"interaction not found: {interaction_id}")
        result = {"interaction": detail}
        if wrapped:
            return _result(rpc_id, {"message": _text_message(
                json.dumps(result, ensure_ascii=False), context_id)})
        return _result(rpc_id, result)

    def _tasks_get(params: dict, rpc_id, *, identity: dict | None = None,
                   context_id: str | None = None,
                   wrapped: bool = False) -> JSONResponse:
        task_id = params.get("id")
        row = None
        if identity and identity.get("kind") == "hub":
            if not isinstance(context_id, str) or not context_id.strip():
                return _error(rpc_id, -32602,
                              "tasks/get 必须携带 contextId")
            from orchestrator import collaboration_store

            try:
                row = collaboration_store.require_a2a_task(
                    tm.conn,
                    task_id=task_id,
                    peer=identity["peer"],
                    context_id=context_id,
                )
            except PermissionError as exc:
                return _error(rpc_id, -32003, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                return _error(rpc_id, -32602, str(exc))
        elif task_id:
            row = state_store.get_task(tm.conn, task_id)
        if row is None:
            return _error(rpc_id, -32602, f"task not found: {task_id}")
        task = _to_a2a(tm.conn, row, context_id=context_id)
        return _result(rpc_id, {"task": task} if wrapped else task)

    return app


app = None  # 惰性：测试用 create_app(自建 tm)；服务进程走 main()


def main() -> None:
    import uvicorn

    host = os.environ.get("LAS_ORCH_BIND", "127.0.0.1")
    validate_orchestrator_security(host)
    global app
    app = create_app()
    uvicorn.run(app, host=host,
                port=int(os.environ.get("LAS_ORCH_PORT", "8310")))


if __name__ == "__main__":
    main()
