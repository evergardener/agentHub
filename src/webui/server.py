"""agentHub Web UI — 看板、审批、会话介入与生产控制面安全。

功能：
  Dashboard   Agent 在线状态/租约 + 任务按状态计数
  任务列表/详情  状态时间线、runs、artifacts、关联事件
  事件流      /api/events/stream（SSE，seq 游标轮询）
  审批中心    blocked 任务批准/拒绝；常驻授权（grants）管理

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
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse,
                               StreamingResponse)

STATIC = Path(__file__).parent / "static"
COOKIE_NAME = "agenthub_session"
ROLES = {"viewer": 1, "operator": 2, "admin": 3}


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
                        else "admin" if request.url.path.startswith("/api/grants")
                        else "operator")
            if ROLES[claims["role"]] < ROLES[required]:
                return JSONResponse(
                    {"error": f"{required} role required"}, status_code=403)
        return await call_next(request)

    # ---------- 页面 ----------

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

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
        conn = _conn()
        try:
            agents = _rows(conn.execute(
                "SELECT id, role, endpoint, status, skills_json,"
                " last_seen_at, lease_expires_at FROM agents ORDER BY id;"))
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
            return {"task": {k: row[k] for k in keys}, "runs": runs,
                    "artifacts": artifacts, "events": events,
                    "interactions": interactions, "sessions": sessions,
                    "messages": messages, "plan_step": plan_step,
                    "plan_steps": plan_steps,
                    "collaboration": collaboration}
        finally:
            conn.close()

    @app.get("/api/tasks/{task_id}/artifact-content")
    def artifact_content(task_id: str, name: str, max_bytes: int = 262144):
        """读取任务产物内容（Web UI 查看执行过程/对话流）。

        安全：仅允许读取工作区根目录内的文件；超出 max_bytes（上限 1MB）
        截断并标记 truncated。
        """
        from common import config as cfg

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
        import os
        roots = [cfg.workspace().resolve()]
        roots += [Path(r).resolve() for r in
                  os.environ.get("LAS_WEBUI_ARTIFACT_ROOTS", "").split(",")
                  if r.strip()]
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
            return {"alerts": list_alerts(conn, status=status, limit=limit)}
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
    def approve(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            status = tm.approve_task(
                task_id, notes=(body or {}).get("notes", "webui"))
            return {"task_id": task_id, "status": status}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"},
                                status_code=409)
        finally:
            tm.close()

    @app.post("/api/tasks/{task_id}/reject")
    def reject(task_id: str, body: dict | None = None):
        from orchestrator.task_manager import TaskManager

        tm = TaskManager()
        try:
            status = tm.reject_task(
                task_id, notes=(body or {}).get("notes", "webui"))
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
