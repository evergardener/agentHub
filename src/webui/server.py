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
            return {"task": {k: row[k] for k in keys}, "runs": runs,
                    "artifacts": artifacts, "events": events}
        finally:
            conn.close()

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
