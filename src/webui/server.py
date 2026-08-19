"""agentHub Web UI — 只读为主 + 审批操作（Evolution v3 §5.2）。

功能：
  Dashboard   Agent 在线状态/租约 + 任务按状态计数
  任务列表/详情  状态时间线、runs、artifacts、关联事件
  事件流      /api/events/stream（SSE，seq 游标轮询）
  审批中心    blocked 任务批准/拒绝；常驻授权（grants）管理

loopback only，无账号体系。启动：
  python -m webui.server   # LAS_WEBUI_HOST/PORT 可覆盖（默认 127.0.0.1:8080）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse,
                               StreamingResponse)

STATIC = Path(__file__).parent / "static"


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
    app = FastAPI(title="agentHub Web UI", version="0.1.0")

    # ---------- 页面 ----------

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

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
            return {"agents": agents, "task_counts": counts, "grants": grants}
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
            messages = (_rows(conn.execute(
                "SELECT id, sender_type, sender_id, recipient_type,"
                " recipient_id, message_type, content_json, sequence,"
                " based_on_revision, created_at FROM conversation_messages"
                " WHERE collaboration_id = ? ORDER BY sequence;",
                (row["collaboration_id"],))) if row["collaboration_id"]
                else [])
            return {"task": {k: row[k] for k in keys}, "runs": runs,
                    "artifacts": artifacts, "events": events,
                    "interactions": interactions, "sessions": sessions,
                    "messages": messages}
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

    @app.get("/api/events/stream")
    async def events_stream(request: Request, after: int = 0):
        async def gen():
            last = after
            while True:
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

    uvicorn.run(create_app(),
                host=os.environ.get("LAS_WEBUI_HOST", "127.0.0.1"),
                port=int(os.environ.get("LAS_WEBUI_PORT", "8080")),
                log_level="info")


if __name__ == "__main__":
    main()
