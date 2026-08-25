"""agentHub Web UI — 看板、审批、会话介入与生产控制面安全。

功能：
  Dashboard   Agent 在线状态/租约 + 任务按状态计数
  任务列表/详情  状态时间线、runs、artifacts、关联事件
  事件流      /api/events/stream（SSE，seq 游标轮询）
  审批中心    委派前门禁与 blocked 原生任务批准/拒绝；常驻授权管理

生产支持 token 登录、签名 HttpOnly session cookie、CSRF 与 RBAC。启动：
  python -m webui.server   # LAS_WEBUI_HOST/PORT 可覆盖（默认 127.0.0.1:8080）
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse,
                               StreamingResponse)
from markdown_it import MarkdownIt

STATIC = Path(__file__).parent / "static"
COOKIE_NAME = "agenthub_session"
ROLES = {"viewer": 1, "operator": 2, "admin": 3}
MARKDOWN = MarkdownIt(
    "commonmark",
    {"html": False, "linkify": False, "typographer": False},
).enable(["strikethrough", "table"])


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign_session(payload: dict, secret: str) -> str:
    encoded = _b64encode(json.dumps(
        payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode(), encoded.encode(),
                         hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def _token_id(token: str) -> str:
    """Non-secret stable subject id used to revoke sessions on token removal."""
    return hashlib.sha256(token.encode()).hexdigest()[:24]


def _verify_session(value: str, secret: str) -> dict | None:
    try:
        encoded, supplied = value.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, _b64encode(expected)):
            return None
        payload = json.loads(_b64decode(encoded))
        if (not isinstance(payload, dict)
                or payload.get("role") not in ROLES
                or not isinstance(payload.get("sub"), str)
                or not isinstance(payload.get("csrf"), str)
                or float(payload.get("exp", 0)) <= time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
        return None


def validate_webui_security(host: str) -> None:
    """Fail startup closed when auth/session configuration is unsafe."""
    import ipaddress

    from common import config as cfg

    tokens = cfg.webui_tokens()
    if cfg.webui_require_auth() and not tokens:
        raise RuntimeError(
            "LAS_WEBUI_REQUIRE_AUTH=true 但 LAS_WEBUI_TOKENS 为空")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback and not tokens:
        raise RuntimeError(
            "WebUI 绑定非 loopback 地址时必须配置 LAS_WEBUI_TOKENS")
    if tokens and len(cfg.webui_session_secret()) < 32:
        raise RuntimeError(
            "启用 WebUI 认证时 LAS_WEBUI_SESSION_SECRET 至少 32 个字符")
    if tokens:
        cfg.webui_session_ttl()


def _conn():
    from state.db import connect

    return connect()  # LAS_DATABASE_URL


def _rows(cur) -> list[dict]:
    out = []
    for r in cur.fetchall():
        keys = r.keys() if hasattr(r, "keys") else None
        out.append({k: r[k] for k in keys} if keys else dict(r))
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _artifact_roots() -> list[Path]:
    """Return the read-only roots the WebUI is allowed to inspect."""
    import os

    from common import config as cfg

    roots = [cfg.workspace().resolve()]
    roots += [Path(root).resolve() for root in
              os.environ.get("LAS_WEBUI_ARTIFACT_ROOTS", "").split(",")
              if root.strip()]
    return roots


def _artifact_availability(artifact: dict, roots: list[Path]) -> dict:
    """Add a non-secret availability hint without reading artifact content."""
    path = Path(artifact["path"]).resolve()
    allowed = any(_is_under(path, root) for root in roots)
    return {
        **artifact,
        "available": allowed and path.is_file(),
        "availability_reason": (
            None if allowed and path.is_file()
            else "path_not_allowed" if not allowed
            else "file_missing"
        ),
    }


def _with_legacy_task_results(messages: list[dict],
                              tasks: list[dict]) -> list[dict]:
    """Expose pre-fix task results in chat without mutating production data."""
    represented = {
        message.get("task_id") for message in messages
        if ("task.result" in str(message.get("message_type", ""))
            or "task.error" in str(message.get("message_type", "")))
    }
    sequence = max(
        (int(message.get("sequence") or 0) for message in messages),
        default=0,
    )
    merged = list(messages)
    for task in tasks:
        task_id = task.get("id")
        if not task_id or task_id in represented:
            continue
        summary = task.get("result_summary")
        error = task.get("error_message")
        text = summary or error
        if not isinstance(text, str) or not text.strip():
            continue
        sequence += 1
        is_error = not bool(summary)
        agent_id = task.get("assigned_to") or "agent"
        merged.append({
            "id": f"legacy-task-result:{task_id}",
            "conversation_id": None,
            "collaboration_id": task.get("collaboration_id"),
            "task_id": task_id,
            "agent_id": agent_id,
            "sender_type": "agent",
            "sender_id": agent_id,
            "recipient_type": "hermes",
            "recipient_id": "hermes",
            "message_type": (
                "agent.task.error.legacy" if is_error
                else "agent.task.result.legacy"
            ),
            "content_json": json.dumps(
                {"text": text, "status": task.get("status"),
                 "legacy": True},
                ensure_ascii=False, separators=(",", ":"),
            ),
            "sequence": sequence,
            "based_on_revision": None,
            "delivery_status": "persisted",
            "created_at": task.get("updated_at") or task.get("created_at"),
        })
    return merged


def _message_text(message: dict) -> str:
    """Extract the human-readable text from a persisted message payload."""
    value = message.get("content_json", "")
    try:
        value = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return str(value or "")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "text"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _with_rendered_markdown(messages: list[dict]) -> list[dict]:
    """Add safe CommonMark HTML while preserving the original message JSON."""
    rendered = []
    for message in messages:
        item = dict(message)
        text = _message_text(item)
        item["content_html"] = MARKDOWN.render(text) if text.strip() else ""
        rendered.append(item)
    return rendered


def _objective_presentation(objective: str) -> tuple[str, str]:
    """Build concise display copy while retaining the full audit objective."""
    normalized = re.sub(r"\s+", " ", str(objective or "")).strip()
    if not normalized:
        return "", ""
    parts = [part.strip() for part in re.split(
        r"(?<=[。！？!?])", normalized) if part.strip()]
    first = parts[0].rstrip("。！？!?")
    title = re.sub(r"^修复\s+agentHub\s+", "修复 ", first,
                   flags=re.IGNORECASE)
    title = re.sub(
        r"GitHub Actions(?:\s+Docker)?\s+发布流水线",
        "GitHub 流水线", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\s*在\s*commit\s+[0-9a-f]{7,64}\s*失败的问题$",
        "构建失败问题", title, flags=re.IGNORECASE)
    if len(title) > 56:
        clause = re.split(r"[，,；;：:]", title, maxsplit=1)[0].strip()
        title = clause if 8 <= len(clause) <= 56 else title[:55].rstrip() + "…"
    summary = "".join(parts[1:]).strip()
    if len(summary) > 180:
        summary = summary[:180].rstrip() + "…"
    return title, summary


def _agent_activity_messages(conn, collaboration_id: str,
                             persisted_messages: list[dict]) -> list[dict]:
    """Project safe native lifecycle events into the conversation transcript."""
    rows = _rows(conn.execute(
        "SELECT e.seq, e.task_id, e.agent_id AS event_agent_id,"
        " e.payload_json, e.created_at, t.assigned_to"
        " FROM events e JOIN tasks t ON t.id = e.task_id"
        " WHERE t.collaboration_id = ?"
        " AND e.event_type = 'agent.session.event' ORDER BY e.seq;",
        (collaboration_id,),
    ))
    persisted_text = {
        _message_text(message).strip() for message in persisted_messages
        if _message_text(message).strip()
    }
    activities: list[dict] = []
    tool_positions: dict[tuple[str, str, str], int] = {}
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("nativeEventType") != "item.lifecycle":
            continue
        data = payload.get("data") or {}
        item = data.get("item") or {}
        phase = data.get("phase")
        item_type = item.get("type")
        item_id = item.get("id")
        agent_id = row.get("event_agent_id") or row.get("assigned_to") or "agent"
        base = {
            "conversation_id": None,
            "collaboration_id": collaboration_id,
            "task_id": row.get("task_id"),
            "agent_id": agent_id,
            "sender_type": "agent",
            "sender_id": agent_id,
            "recipient_type": "hermes",
            "recipient_id": "hermes",
            "sequence": f"event-{row['seq']}",
            "based_on_revision": None,
            "delivery_status": "event",
            "created_at": row.get("created_at"),
        }
        if item_type == "agentMessage":
            text = item.get("text")
            if (phase != "completed" or not isinstance(text, str)
                    or not text.strip() or text.strip() in persisted_text):
                continue
            activities.append({
                **base,
                "id": f"native-message:{row['seq']}",
                "message_type": "agent.activity.message",
                "content_json": json.dumps(
                    {"text": text, "phase": item.get("phase")},
                    ensure_ascii=False, separators=(",", ":")),
            })
            continue
        if item_type not in {"commandExecution", "fileChange"}:
            continue
        safe_item = {key: value for key, value in item.items()
                     if key not in {"id", "type"}}
        content_json = json.dumps({
            "tool_calls": [{"name": item_type, "arguments": {
                **safe_item, "lifecycle": phase,
            }}],
        }, ensure_ascii=False, separators=(",", ":"))
        key = (str(row.get("task_id")), str(item_id), str(item_type))
        existing = tool_positions.get(key)
        if existing is not None:
            activities[existing]["content_json"] = content_json
            continue
        tool_positions[key] = len(activities)
        activities.append({
            **base,
            "id": f"native-tool:{row['seq']}",
            "message_type": "agent.activity.tool",
            "content_json": content_json,
        })
    return activities


def create_app() -> FastAPI:
    from common import config as cfg

    # create_app is used by tests and local embedding; validate token/session
    # invariants here, while main() additionally validates the actual bind host.
    tokens = cfg.webui_tokens()
    if tokens and len(cfg.webui_session_secret()) < 32:
        raise RuntimeError(
            "启用 WebUI 认证时 LAS_WEBUI_SESSION_SECRET 至少 32 个字符")
    if cfg.webui_require_auth() and not tokens:
        raise RuntimeError(
            "LAS_WEBUI_REQUIRE_AUTH=true 但 LAS_WEBUI_TOKENS 为空")
    session_secret = cfg.webui_session_secret()
    valid_subjects = {_token_id(token): role for token, role in tokens.items()}
    app = FastAPI(title="agentHub Web UI", version="0.1.0")

    @app.middleware("http")
    async def control_plane_security(request: Request, call_next):
        if not tokens:
            request.state.role = "admin"
            return await call_next(request)

        # The login form and shell must be reachable before a session exists.
        if request.url.path in {"/", "/health", "/ready",
                                "/api/auth/login"}:
            return await call_next(request)

        claims = _verify_session(
            request.cookies.get(COOKIE_NAME, ""), session_secret)
        if (claims is None
                or valid_subjects.get(claims.get("sub")) != claims.get("role")):
            return JSONResponse({"error": "authentication required"},
                                status_code=401)
        request.state.role = claims["role"]
        request.state.session_exp = claims["exp"]

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf, claims["csrf"]):
                return JSONResponse({"error": "CSRF token invalid"},
                                    status_code=403)
            required = ("viewer" if request.url.path == "/api/auth/logout"
                        else "admin" if request.url.path.startswith(
                            ("/api/grants", "/api/agents"))
                        else "operator")
            if ROLES[claims["role"]] < ROLES[required]:
                return JSONResponse(
                    {"error": f"{required} role required"}, status_code=403)
        return await call_next(request)

    # ---------- 页面 ----------

    @app.get("/")
    async def index():
        # The console is a single deploy-time HTML bundle. Never let a browser
        # keep an older bootstrap/login implementation across a container
        # rollout; API responses and session cookies remain independently
        # controlled by their own routes.
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "webui"}

    @app.get("/ready")
    async def ready():
        try:
            conn = _conn()
            try:
                conn.execute("SELECT 1;").fetchone()
            finally:
                conn.close()
            return {"status": "ready", "service": "webui"}
        except Exception:
            return JSONResponse({"status": "not-ready"}, status_code=503)

    # ---------- 登录 / 会话 ----------

    @app.post("/api/auth/login")
    async def login(request: Request):
        if not tokens:
            return {"enabled": False, "role": "admin", "csrf": None}
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        supplied = body.get("token", "") if isinstance(body, dict) else ""
        role = None
        if isinstance(supplied, str):
            # Compare every configured token to avoid a dictionary timing oracle.
            for candidate, candidate_role in tokens.items():
                if hmac.compare_digest(supplied, candidate):
                    role = candidate_role
        if role is None:
            return JSONResponse({"error": "invalid login token"},
                                status_code=401)
        csrf = secrets.token_urlsafe(24)
        response = JSONResponse({"enabled": True, "role": role,
                                 "csrf": csrf})
        response.set_cookie(
            COOKIE_NAME,
            _sign_session({"role": role, "sub": _token_id(supplied),
                           "csrf": csrf,
                           "exp": int(time.time()) + cfg.webui_session_ttl()},
                          session_secret),
            max_age=cfg.webui_session_ttl(), httponly=True,
            secure=cfg.webui_cookie_secure(), samesite="strict", path="/",
        )
        return response

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        if not tokens:
            return {"enabled": False, "role": "admin", "csrf": None}
        claims = _verify_session(
            request.cookies.get(COOKIE_NAME, ""), session_secret)
        # Middleware has already authenticated this request; keep the explicit
        # check so this route remains safe if middleware exclusions change.
        if (claims is None
                or valid_subjects.get(claims.get("sub")) != claims.get("role")):
            return JSONResponse({"error": "authentication required"},
                                status_code=401)
        return {"enabled": True, "role": claims["role"],
                "csrf": claims["csrf"]}

    @app.post("/api/auth/logout")
    async def logout():
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(COOKIE_NAME, path="/", samesite="strict")
        return response

    # ---------- 只读 API ----------

    @app.get("/api/overview")
    def overview():
        from datetime import datetime

        from hermes.tools import load_agents
        from orchestrator import agent_control_store
        from state.db import CST

        conn = _conn()
        try:
            live_rows = _rows(conn.execute(
                "SELECT id, role, endpoint, status, skills_json,"
                " last_seen_at, lease_expires_at FROM agents ORDER BY id;"))
            live = {row["id"]: row for row in live_rows}
            catalog = load_agents()
            now = datetime.now(CST).isoformat(timespec="seconds")
            agents = []
            # The production catalog is authoritative. Registry-only rows are
            # commonly integration-test workers (for example `fake`) and must
            # not be presented as deployable Agents in the control console.
            for agent_id, spec in catalog.items():
                row = live.get(agent_id) or {}
                enabled = agent_control_store.desired_enabled(
                    conn, agent_id, spec.get("enabled", True) is not False)
                online = bool(
                    enabled and row.get("lease_expires_at")
                    and row["lease_expires_at"] > now
                )
                agents.append({
                    "id": agent_id,
                    "role": row.get("role") or spec.get("role", "worker"),
                    "endpoint": row.get("endpoint") or spec.get("endpoint"),
                    "status": ("online" if online else
                               "disabled" if not enabled else "offline"),
                    "enabled": enabled,
                    "online": online,
                    "skills_json": row.get("skills_json")
                    or json.dumps(spec.get("skills", []), ensure_ascii=False),
                    "last_seen_at": row.get("last_seen_at"),
                    "lease_expires_at": row.get("lease_expires_at"),
                })
            counts = {
                r["status"]: r["n"] for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status;")
            }
            grants = _rows(conn.execute(
                "SELECT id, pattern, note, created_at FROM approval_grants"
                " WHERE revoked_at IS NULL ORDER BY id;"))
            alert_counts = {
                r["severity"]: r["n"] for r in conn.execute(
                    "SELECT severity, COUNT(*) AS n FROM alerts"
                    " WHERE status = 'open' GROUP BY severity;")
            }
            return {"agents": agents, "task_counts": counts, "grants": grants,
                    "alert_counts": alert_counts}
        finally:
            conn.close()

    @app.patch("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, request: Request):
        from hermes.tools import load_agents
        from orchestrator import agent_control_store

        catalog = load_agents()
        if agent_id not in catalog:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return JSONResponse(
                {"error": "enabled must be boolean"}, status_code=400)
        conn = _conn()
        try:
            return agent_control_store.set_enabled(
                conn, agent_id=agent_id, enabled=body["enabled"],
                updated_by=f"webui:{request.state.role}")
        finally:
            conn.close()

    @app.get("/api/tasks")
    def tasks(status: str | None = None, limit: int = 200):
        from orchestrator.state_store import list_tasks

        conn = _conn()
        try:
            rows = list_tasks(conn, status=status)[:limit]
            keys = rows[0].keys() if rows else []
            return {"tasks": [{k: r[k] for k in keys} for r in rows]}
        finally:
            conn.close()

    @app.get("/api/approvals")
    def approvals(limit: int = 200):
        """List both pre-delegation gates and blocked native agent turns."""
        conn = _conn()
        try:
            rows = _rows(conn.execute(
                "SELECT t.* FROM tasks t WHERE ("
                " t.status IN ('created','queued') AND"
                " (SELECT e.event_type FROM events e"
                "  WHERE e.task_id = t.id AND e.event_type IN (?,?,?)"
                "  ORDER BY e.seq DESC LIMIT 1) = ?"
                ") ORDER BY t.created_at DESC LIMIT ?;",
                ("task.approval_requested", "task.approved",
                 "task.rejected", "task.approval_requested",
                 min(max(int(limit), 1), 500)),
            ))
            for row in rows:
                if row["status"] in {"created", "queued"}:
                    row["status"] = "input_required"
                    row["approval_kind"] = "delegation"
                else:
                    row["approval_kind"] = "native"
            return {"tasks": rows}
        finally:
            conn.close()

    @app.get("/api/acceptance")
    def acceptance(limit: int = 200):
        """List results that require an explicit user acceptance decision."""
        conn = _conn()
        try:
            return {"tasks": _rows(conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?)"
                " ORDER BY updated_at DESC LIMIT ?;",
                ("awaiting_acceptance", "reviewed",
                 min(max(int(limit), 1), 500)),
            ))}
        finally:
            conn.close()

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        from orchestrator.state_store import get_task

        conn = _conn()
        try:
            row = get_task(conn, task_id)
            if row is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            keys = row.keys()
            runs = _rows(conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ?"
                " ORDER BY started_at;", (task_id,)))
            artifacts = _rows(conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ?"
                " ORDER BY created_at;", (task_id,)))
            artifacts = [
                _artifact_availability(artifact, _artifact_roots())
                for artifact in artifacts
            ]
            events = _rows(conn.execute(
                "SELECT seq, event_type, payload_json, created_at FROM events"
                " WHERE task_id = ? ORDER BY seq;", (task_id,)))
            interactions = _rows(conn.execute(
                "SELECT i.*, a.operation, a.risk, a.policy_route,"
                " a.policy_reason, a.status AS action_intent_status"
                " FROM agent_session_interactions i"
                " LEFT JOIN action_intents a ON a.id = i.action_intent_id"
                " WHERE i.task_id = ? ORDER BY i.requested_at;",
                (task_id,)))
            sessions = _rows(conn.execute(
                "SELECT * FROM agent_session_bindings WHERE task_id = ?"
                " ORDER BY is_current DESC, created_at DESC;", (task_id,)))
            plan_step_row = conn.execute(
                "SELECT s.*, t.status AS task_status, p.revision AS plan_revision,"
                " p.status AS plan_status, p.objective AS plan_objective,"
                " p.based_on_revision AS plan_context_revision"
                " FROM task_plan_steps s JOIN task_plans p ON p.id = s.plan_id"
                " JOIN tasks t ON t.id = s.task_id"
                " WHERE s.task_id = ?;", (task_id,),
            ).fetchone()
            plan_step = ({key: plan_step_row[key]
                          for key in plan_step_row.keys()}
                         if plan_step_row else None)
            plan_steps = (_rows(conn.execute(
                "SELECT s.step_key, s.ordinal, s.task_id, s.objective,"
                " s.agent_id, s.profile_id, s.profile_version,"
                " s.depends_on_json,"
                " t.status AS task_status"
                " FROM task_plan_steps s JOIN tasks t ON t.id = s.task_id"
                " WHERE s.plan_id = ? ORDER BY s.ordinal;",
                (plan_step_row["plan_id"],))) if plan_step_row else [])
            messages = (_rows(conn.execute(
                "SELECT id, sender_type, sender_id, recipient_type,"
                " recipient_id, message_type, content_json, sequence,"
                " based_on_revision, created_at FROM conversation_messages"
                " WHERE collaboration_id = ? ORDER BY sequence;",
                (row["collaboration_id"],))) if row["collaboration_id"]
                else [])
            collaboration_row = (conn.execute(
                "SELECT id, phase, controller, context_revision, updated_at"
                " FROM collaborations WHERE id = ?;",
                (row["collaboration_id"],),
            ).fetchone() if row["collaboration_id"] else None)
            collaboration = ({key: collaboration_row[key]
                              for key in collaboration_row.keys()}
                             if collaboration_row else None)
            dispatch_row = conn.execute(
                "SELECT content_json, message_type"
                " FROM conversation_messages WHERE task_id = ?"
                " AND message_type IN (?, ?) ORDER BY sequence LIMIT 1;",
                (task_id, "a2a.task.request",
                 "a2a.task.request.historical"),
            ).fetchone()
            dispatch_objective = (
                _message_text({key: dispatch_row[key]
                               for key in dispatch_row.keys()})
                if dispatch_row else ""
            ).strip()
            if dispatch_objective:
                objective = dispatch_objective
                instruction_source = "a2a_task_request"
            elif plan_step and str(plan_step.get("objective") or "").strip():
                objective = str(plan_step["objective"]).strip()
                instruction_source = "task_plan_step"
            else:
                objective = row["objective"]
                instruction_source = "task_record"
            objective_title, objective_summary = _objective_presentation(
                objective)
            try:
                task_context = json.loads(row["plan_context_json"] or "null")
            except (TypeError, ValueError):
                task_context = None
            if isinstance(task_context, dict):
                structured_title = task_context.get("display_title")
                structured_summary = task_context.get("objective_summary")
                if isinstance(structured_title, str) and structured_title.strip():
                    objective_title = structured_title.strip()
                if (isinstance(structured_summary, str)
                        and structured_summary.strip()):
                    objective_summary = structured_summary.strip()
            return {"task": {k: row[k] for k in keys}, "runs": runs,
                    "artifacts": artifacts, "events": events,
                    "interactions": interactions, "sessions": sessions,
                    "messages": messages, "plan_step": plan_step,
                    "plan_steps": plan_steps,
                    "collaboration": collaboration,
                    "dispatched_objective": objective,
                    "objective_title": objective_title,
                    "objective_summary": objective_summary,
                    "instruction_source": instruction_source}
        finally:
            conn.close()

    @app.get("/api/collaborations")
    def collaborations(limit: int = 50):
        """List durable Hermes conversations for live audit and resume checks."""
        limit = min(max(int(limit), 1), 200)
        conn = _conn()
        try:
            rows = _rows(conn.execute(
                "SELECT co.id, co.conversation_id, co.objective, co.status,"
                " co.phase, co.controller, co.context_revision, co.created_at,"
                " co.updated_at, c.title, c.project,"
                " c.updated_at AS conversation_updated_at,"
                " c.status AS conversation_status,"
                " (SELECT COUNT(*) FROM conversation_messages m"
                "   WHERE m.collaboration_id = co.id) AS message_count,"
                " (SELECT COUNT(*) FROM tasks t"
                "   WHERE t.collaboration_id = co.id) AS task_count"
                " FROM collaborations co JOIN conversations c"
                " ON c.id = co.conversation_id"
                " ORDER BY c.updated_at DESC, co.updated_at DESC LIMIT ?;",
                (limit,)))
            for row in rows:
                row["assigned_agents"] = [
                    item["assigned_to"] for item in _rows(conn.execute(
                        "SELECT DISTINCT assigned_to FROM tasks"
                        " WHERE collaboration_id = ?"
                        " AND assigned_to IS NOT NULL"
                        " ORDER BY assigned_to;", (row["id"],)))
                ]
            return {"collaborations": rows}
        finally:
            conn.close()

    @app.patch("/api/collaborations/{collaboration_id}")
    async def update_collaboration(collaboration_id: str, request: Request):
        """Update user-owned presentation metadata without changing context."""
        from orchestrator import state_store
        from state.db import now_iso

        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str) or not title.strip():
            return JSONResponse({"error": "title required"}, status_code=400)
        title = title.strip()
        if len(title) > 100:
            return JSONResponse(
                {"error": "title must contain at most 100 characters"},
                status_code=400,
            )
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT conversation_id FROM collaborations WHERE id = ?;",
                (collaboration_id,),
            ).fetchone()
            if row is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            updated_at = now_iso()
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ?"
                " WHERE id = ?;",
                (title, updated_at, row["conversation_id"]),
            )
            state_store.record_event(conn, {
                "event_id": f"conversation-title-{secrets.token_hex(12)}",
                "event_type": "conversation.title.updated",
                "source": f"webui:{request.state.role}",
                "payload": {
                    "collaboration_id": collaboration_id,
                    "conversation_id": row["conversation_id"],
                    "title": title,
                },
            }, commit=False)
            conn.commit()
            return {
                "collaboration_id": collaboration_id,
                "conversation_id": row["conversation_id"],
                "title": title,
                "updated_at": updated_at,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @app.get("/api/collaborations/{collaboration_id}")
    def collaboration_detail(collaboration_id: str):
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT co.*, c.title, c.project,"
                " c.status AS conversation_status"
                " FROM collaborations co JOIN conversations c"
                " ON c.id = co.conversation_id WHERE co.id = ?;",
                (collaboration_id,),
            ).fetchone()
            if row is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            collaboration = {key: row[key] for key in row.keys()}
            messages = _rows(conn.execute(
                "SELECT id, conversation_id, collaboration_id, task_id,"
                " agent_id, sender_type, sender_id, recipient_type,"
                " recipient_id, message_type, content_json, sequence,"
                " based_on_revision, delivery_status, created_at"
                " FROM conversation_messages WHERE collaboration_id = ?"
                " ORDER BY sequence;", (collaboration_id,)))
            tasks = _rows(conn.execute(
                "SELECT id, objective, status, assigned_to, collaboration_id,"
                " result_summary, error_message, created_at, updated_at"
                " FROM tasks WHERE collaboration_id = ?"
                " ORDER BY created_at;", (collaboration_id,)))
            sessions = _rows(conn.execute(
                "SELECT id, task_id, agent_id, native_session_id, status,"
                " resume_capability, context_revision, is_current,"
                " created_at, last_active_at FROM agent_session_bindings"
                " WHERE collaboration_id = ? ORDER BY created_at;",
                (collaboration_id,)))
            merged_messages = _with_legacy_task_results(messages, tasks)
            agent_activity = _agent_activity_messages(
                conn, collaboration_id, merged_messages)
            return {"collaboration": collaboration,
                    "messages": _with_rendered_markdown(merged_messages),
                    "agent_activity": _with_rendered_markdown(agent_activity),
                    "tasks": tasks, "sessions": sessions}
        finally:
            conn.close()

    @app.post("/api/collaborations/{collaboration_id}/messages")
    async def add_collaboration_message(collaboration_id: str, body: dict):
        """Persist a WebUI user message to Hermes without requiring a task."""
        from orchestrator import collaboration_store

        recipient_id = (body or {}).get("recipient_id", "hermes")
        if recipient_id != "hermes":
            return JSONResponse(
                {"error": "collaboration messages must target hermes"},
                status_code=400,
            )
        text = (body or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "text required"}, status_code=400)
        text = text.strip()
        if len(text) > 20000:
            return JSONResponse(
                {"error": "text must contain at most 20000 characters"},
                status_code=400,
            )
        raw_key = (body or {}).get("idempotency_key")
        idempotency_key = (
            f"webui-collaboration:{collaboration_id}:{raw_key}"
            if isinstance(raw_key, str) and raw_key else None
        )
        conn = _conn()
        try:
            if collaboration_store.get_collaboration(
                    conn, collaboration_id) is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            message = collaboration_store.record_user_intervention(
                conn,
                collaboration_id=collaboration_id,
                user_id="user",
                mode="comment",
                content={"text": text},
                idempotency_key=idempotency_key,
            )
            return {
                "collaboration_id": collaboration_id,
                "recipient_id": "hermes",
                "message_id": message["id"],
                "sequence": message["sequence"],
                "context_revision": message["based_on_revision"],
            }
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        finally:
            conn.close()

    @app.get("/api/tasks/{task_id}/artifact-content")
    def artifact_content(task_id: str, name: str, max_bytes: int = 262144):
        """读取任务产物内容（Web UI 查看执行过程/对话流）。

        安全：仅允许读取工作区根目录内的文件；超出 max_bytes（上限 1MB）
        截断并标记 truncated。
        """
        max_bytes = min(max_bytes, 1024 * 1024)
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT path FROM artifacts WHERE task_id = ? AND name = ?"
                " ORDER BY created_at DESC LIMIT 1;", (task_id, name)).fetchone()
        finally:
            conn.close()
        if row is None:
            return JSONResponse({"error": "artifact not found"},
                                status_code=404)
        p = Path(row["path"]).resolve()
        # 允许的根：容器工作区 + LAS_WEBUI_ARTIFACT_ROOTS（宿主机工作区，
        # host adapter 写的产物路径以宿主机绝对路径入库，compose 以相同
        # 路径只读挂载进来，见 docker-compose.yml webui 服务）
        roots = _artifact_roots()
        if not any(_is_under(p, root) for root in roots):
            return JSONResponse({"error": "artifact 路径不在允许的工作区内"},
                                status_code=403)
        if not p.is_file():
            return JSONResponse({"error": f"文件不存在: {p}"},
                                status_code=404)
        data = p.read_bytes()
        return {"name": name, "path": str(p), "size": len(data),
                "truncated": len(data) > max_bytes,
                "content": data[:max_bytes].decode("utf-8", errors="replace")}

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 100):
        conn = _conn()
        try:
            return {"events": _rows(conn.execute(
                "SELECT seq, event_type, task_id, agent_id, payload_json,"
                " created_at FROM events WHERE seq > ?"
                " ORDER BY seq LIMIT ?;", (after, limit)))}
        finally:
            conn.close()

    @app.get("/api/alerts")
    def alerts(status: str | None = "open", limit: int = 200):
        from state.alert_store import list_alerts

        conn = _conn()
        try:
            rows = list_alerts(conn, status=status, limit=limit)
            task_ids = {row["task_id"] for row in rows if row.get("task_id")}
            existing = set()
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                existing = {row["id"] for row in conn.execute(
                    f"SELECT id FROM tasks WHERE id IN ({placeholders});",
                    tuple(task_ids),
                ).fetchall()}
            for row in rows:
                row["task_exists"] = bool(
                    row.get("task_id") and row["task_id"] in existing)
            return {"alerts": rows}
        finally:
            conn.close()

    @app.post("/api/alerts/{alert_id}/acknowledge")
    def acknowledge_alert(alert_id: str, body: dict, request: Request):
        from state.alert_store import acknowledge_alert

        note = body.get("note", "") if isinstance(body, dict) else ""
        conn = _conn()
        try:
            changed = acknowledge_alert(
                conn, alert_id, actor=f"webui:{request.state.role}", note=note)
        finally:
            conn.close()
        if not changed:
            return JSONResponse(
                {"error": "alert not found or already acknowledged"},
                status_code=409)
        return {"id": alert_id, "status": "acknowledged"}

    @app.get("/api/events/stream")
    async def events_stream(request: Request, after: int = 0):
        async def gen():
            last = after
            while True:
                session_exp = getattr(request.state, "session_exp", None)
                if session_exp is not None and time.time() >= session_exp:
                    break
                # 客户端断开即退出，避免孤儿生成器每 3s 空转查库
                if await request.is_disconnected():
                    break
                conn = _conn()
                try:
                    rows = _rows(conn.execute(
                        "SELECT seq, event_type, task_id, agent_id,"
                        " payload_json, created_at FROM events"
                        " WHERE seq > ? ORDER BY seq LIMIT 200;", (last,)))
                finally:
                    conn.close()
                for r in rows:
                    last = r["seq"]
                    yield f"data: {json.dumps(r, ensure_ascii=False)}\n\n"
                # 无新事件时发注释保活帧，代理/浏览器不断连
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---------- 审批操作 ----------

    @app.get("/api/interactions")
    def interactions(status: str | None = "pending", limit: int = 200):
        conn = _conn()
        try:
            where = " WHERE i.status = ?" if status else ""
            params = (status, limit) if status else (limit,)
            return {"interactions": _rows(conn.execute(
                "SELECT i.*, a.operation, a.risk, a.policy_route,"
                " a.policy_reason, a.status AS action_intent_status"
                " FROM agent_session_interactions i"
                " LEFT JOIN action_intents a ON a.id = i.action_intent_id"
                + where + " ORDER BY i.requested_at LIMIT ?;", params))}
        finally:
            conn.close()

    @app.post("/api/interactions/{interaction_id}/respond")
    async def respond_interaction(interaction_id: str, body: dict):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            result = await tm.respond_agent_interaction(
                interaction_id,
                response=body or {},
                requested_by="user",
            )
            return {"interaction_id": interaction_id, "task": result}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/interventions")
    async def intervene(task_id: str, body: dict):
        from orchestrator.task_manager import TaskManager

        mode = (body or {}).get("mode")
        content = (body or {}).get("content", "")
        if not isinstance(mode, str) or not mode:
            return JSONResponse({"error": "mode required"}, status_code=400)
        tm = TaskManager()
        try:
            result = await tm.intervene_agent_session(
                task_id, mode=mode, content=content,
                agent_id=(body or {}).get("agent_id"),
                user_id="user",
                idempotency_key=(body or {}).get("idempotency_key"),
            )
            return result
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/approve")
    async def approve(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            status = await tm.approve_task_request(
                task_id, notes=(body or {}).get("notes", "webui"),
                decided_by="user", via="webui")
            return {"task_id": task_id, "status": status}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/reject")
    async def reject(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            status = await tm.reject_task_request(
                task_id, notes=(body or {}).get("notes", "webui"),
                decided_by="user", via="webui")
            return {"task_id": task_id, "status": status}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/accept")
    def accept_result(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            status = tm.accept_result(
                task_id, notes=(body or {}).get("notes", ""),
                decided_by="user", via="webui")
            return {"task_id": task_id, "status": status}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/request-rework")
    def request_rework(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        feedback = (body or {}).get("feedback", "")
        if not isinstance(feedback, str) or not feedback.strip():
            return JSONResponse(
                {"error": "feedback required"}, status_code=409)
        tm = TaskManager()
        try:
            status = tm.reject_result(
                task_id, feedback=feedback, decided_by="user", via="webui")
            return {"task_id": task_id, "status": status}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    # ---------- 常驻授权 ----------

    @app.post("/api/grants")
    def grant(body: dict):
        from hermes.policy import ApprovalPolicy

        pattern = (body or {}).get("pattern", "").strip()
        if not pattern:
            return JSONResponse({"error": "pattern required"}, status_code=400)
        policy = ApprovalPolicy()
        if any(k in pattern for k in policy.never_grant):
            return JSONResponse({"error": "never_grant 类不允许常驻授权"},
                                status_code=400)
        conn = _conn()
        try:
            gid = ApprovalPolicy.grant(conn, pattern,
                                       note=(body or {}).get("note", "webui"))
            return {"grant_id": gid, "pattern": pattern}
        finally:
            conn.close()

    @app.post("/api/grants/{grant_id}/revoke")
    def revoke(grant_id: int):
        from hermes.policy import ApprovalPolicy

        conn = _conn()
        try:
            ok = ApprovalPolicy.revoke(conn, grant_id)
            return {"grant_id": grant_id, "revoked": ok}
        finally:
            conn.close()

    return app


def main() -> None:
    import os

    import uvicorn

    host = os.environ.get("LAS_WEBUI_HOST", "127.0.0.1")
    validate_webui_security(host)
    uvicorn.run(create_app(),
                host=host,
                port=int(os.environ.get("LAS_WEBUI_PORT", "8080")),
                log_level="info")


if __name__ == "__main__":
    main()
