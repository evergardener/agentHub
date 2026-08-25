"""Phase 4 验收：Hermes Orchestrator 全流程（设计文档 §20 Phase 4）。

真实组件：uvicorn 跑 fake adapter（HTTP）+ 真实 nats-server + State Writer
durable consumer + TaskManager。

流程：create → delegate → wait（事件驱动）→ review 拒绝 → 返工 → 接受。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.anyio

NATS_BIN = shutil.which("nats-server")
NATS_PORT = 14224
NATS_URL = f"nats://127.0.0.1:{NATS_PORT}"
ADAPTER_PORT = 8298
ADAPTER_URL = f"http://127.0.0.1:{ADAPTER_PORT}"

requires_nats = pytest.mark.skipif(not NATS_BIN, reason="nats-server not installed")


def _start_nats(store_dir: Path) -> subprocess.Popen:
    import socket

    proc = subprocess.Popen(
        [NATS_BIN, "-js", "-p", str(NATS_PORT), "--store_dir", str(store_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", NATS_PORT), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("nats-server did not start")


def _start_adapter():
    from adapters.fake.server import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1",
                            port=ADAPTER_PORT, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{ADAPTER_URL}/health", timeout=1).status_code == 200:
                return srv, thread
        except httpx.TransportError:
            time.sleep(0.1)
    raise RuntimeError("adapter did not start")


@requires_nats
async def test_phase4_orchestrator_flow(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", NATS_URL)
    db_path = tmp_path / "agent-state.db"

    nats_proc = _start_nats(tmp_path / "jetstream")
    adapter_srv, adapter_thread = _start_adapter()
    writer_stop = asyncio.Event()
    writer_task = None
    try:
        # State Writer 作为 durable consumer 常驻
        from orchestrator.nats_client import durable_consume, ensure_stream
        from state.writer import StateWriter

        await ensure_stream(NATS_URL)
        writer = StateWriter(db_path)
        writer_task = asyncio.create_task(
            durable_consume("state-writer", lambda e: _apply(writer, e),
                            NATS_URL, stop_event=writer_stop)
        )

        from orchestrator.task_manager import TaskManager

        tm = TaskManager(db_path=db_path, workspace=ws)

        # ── 创建 + 委派 ──
        task_id = tm.create_task("phase4: build something", project="las")
        call = await tm.delegate_task(task_id, ADAPTER_URL, agent_id="fake")

        # ── 事件驱动等待 ──
        final = await tm.wait_task(task_id, timeout=30, nats_url=NATS_URL)
        await call  # A2A 调用收尾
        assert final == "awaiting_acceptance", final

        # State Writer 落库确认
        await asyncio.sleep(0.5)
        row = tm.conn.execute(
            "SELECT status, result_summary FROM tasks WHERE id = ?;",
            (task_id,)).fetchone()
        assert row["status"] == "awaiting_acceptance"

        # ── Hermes 建议返工 → 用户明确返工 → 再完成 → 用户明确接受 ──
        assert tm.review_result(
            task_id, approved=False, notes="redo") == "reviewed"
        assert tm.reject_result(
            task_id, feedback="redo", decided_by="user",
            via="integration-test") == "rework_pending"
        state = tm.conn.execute(
            "SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        assert state[0] == "rework_pending"
        # fake adapter is intentionally one-shot; model a valid second worker
        # attempt through the authoritative state transitions.
        from common.models import TaskStatus
        from orchestrator import state_store

        state_store.transition_task(tm.conn, task_id, TaskStatus.ASSIGNED)
        state_store.transition_task(tm.conn, task_id, TaskStatus.WORKING)
        state_store.transition_task(
            tm.conn, task_id, TaskStatus.AWAITING_ACCEPTANCE)
        assert tm.review_result(task_id, approved=True) == "reviewed"
        assert tm.accept_result(
            task_id, decided_by="user", via="integration-test") == "accepted"
    finally:
        writer_stop.set()
        if writer_task:
            await asyncio.wait_for(writer_task, timeout=5)
        adapter_srv.should_exit = True
        adapter_thread.join(timeout=5)
        nats_proc.terminate()
        nats_proc.wait(timeout=10)


async def _apply(writer, event: dict) -> None:
    writer.apply(event)
