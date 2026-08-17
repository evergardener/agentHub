"""Phase 3 验收：SQLite State Plane（设计文档 §20 Phase 3）。

全链路：Hermes 建任务（counters ID）→ 委派 fake worker（携带 taskId）→
Adapter 发事件 → State Writer 落库 → agentctl 可查；
另验证非法迁移事件被拒绝 + audit 留痕。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import nats
import pytest

from common.models import TaskStatus
from orchestrator import state_store
from state.db import init_db, next_task_id
from state.writer import StateWriter

pytestmark = pytest.mark.anyio

NATS_BIN = shutil.which("nats-server")
TEST_PORT = 14223
TEST_URL = f"nats://127.0.0.1:{TEST_PORT}"

requires_nats = pytest.mark.skipif(not NATS_BIN, reason="nats-server not installed")


def _start_nats(store_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [NATS_BIN, "-js", "-p", str(TEST_PORT), "--store_dir", str(store_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import socket

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", TEST_PORT), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("nats-server did not start")


async def _fetch_events(durable: str) -> list[dict]:
    nc = await nats.connect(TEST_URL, connect_timeout=2,
                            max_reconnect_attempts=1, allow_reconnect=False)
    events = []
    try:
        sub = nc.jetstream()
        sub = await sub.pull_subscribe(">", durable=durable, stream="AGENT_EVENTS")
        while True:
            try:
                msgs = await sub.fetch(batch=50, timeout=1)
            except nats.errors.TimeoutError:
                break
            for m in msgs:
                events.append(json.loads(m.data.decode("utf-8")))
                await m.ack()
    finally:
        await nc.close()
    return events


@requires_nats
async def test_state_plane_end_to_end(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", TEST_URL)
    db_path = tmp_path / "agent-state.db"

    from orchestrator.nats_client import ensure_stream

    proc = _start_nats(tmp_path / "jetstream")
    try:
        await ensure_stream(TEST_URL)

        # ── Hermes 建任务（counters ID，§22.1）──
        conn = init_db(db_path)
        task_id = next_task_id(conn)
        state_store.create_task(
            conn, task_id=task_id, objective="phase3 acceptance task",
            created_by="hermes", project="local-agent-system",
            idempotency_key=f"{task_id}:1",
        )
        state_store.transition_task(conn, task_id, TaskStatus.ASSIGNED)

        # ── 委派 fake worker，携带 Hermes 分配的 taskId ──
        from adapters.fake.server import create_app

        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=60
        ) as c:
            r = await c.post("/a2a", json={
                "jsonrpc": "2.0", "id": "p3", "method": "message/send",
                "params": {"message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "phase3 acceptance task"}],
                    "metadata": {"taskId": task_id,
                                 "idempotencyKey": f"{task_id}:1",
                                 "traceId": "trace-p3"},
                }},
            })
            a2a_task = r.json()["result"]
            from .poll import wait_terminal
            a2a_task = await wait_terminal(c, a2a_task["id"])
        assert a2a_task["id"] == task_id
        assert a2a_task["status"]["state"] == "completed"

        # ── State Writer 消费落库 ──
        writer = StateWriter(db_path)
        events = await _fetch_events("p3-test")
        task_events = [e for e in events if e.get("task_id") == task_id]
        results = [writer.apply(e) for e in task_events]
        assert "rejected" not in results

        row = state_store.get_task(writer.conn, task_id)
        assert row["status"] == "completed"
        assert row["result_summary"]
        arts = writer.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ?;", (task_id,)).fetchall()
        assert arts and arts[0]["sha256"]
        runs = writer.conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ?;", (task_id,)).fetchall()
        assert runs

        # ── 事件重放去重（§17.6）──
        again = writer.apply(task_events[0])
        assert again == "duplicate"

        # ── 非法迁移拒绝 + audit（§5.3）──
        illegal = {
            "event_id": "E-illegal-1", "event_type": "task.started",
            "timestamp": "2026-08-17T14:00:00+08:00", "source": "codex",
            "task_id": task_id,
            "payload": {"status_from": "assigned", "status_to": "working",
                        "attempt": 2},
        }
        assert writer.apply(illegal) == "rejected"  # completed 后不得回 working
        assert writer.audit_log, "illegal transition must leave audit record"

        # ── agentctl 查询路径（直接调 cmd 函数）──
        from cli.agentctl import cmd_task_list, cmd_task_show

        assert cmd_task_list(db_path, None) == 0
        assert cmd_task_show(db_path, task_id) == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)
