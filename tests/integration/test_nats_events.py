"""Phase 2 验收：NATS 事件面 — 中断暂存与恢复重放（设计文档 §17.7 / §20 Phase 2）。

场景：
1. NATS 在线：fake worker 完成任务 → JetStream 可查到 task.started/completed
2. NATS 停止：再完成任务 → 事件写入本地 spool
3. NATS 重启：replay_spool → 暂存事件进入 JetStream，durable consumer 可消费
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import nats
import pytest

pytestmark = pytest.mark.anyio

NATS_BIN = shutil.which("nats-server")

requires_nats = pytest.mark.skipif(not NATS_BIN, reason="nats-server not installed")


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_nats(store_dir: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [NATS_BIN, "-js", "-p", str(port), "--store_dir", str(store_dir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_nats_up(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("nats-server did not start")


async def _fetch_all_events(durable: str, nats_url: str) -> list[dict]:
    nc = await nats.connect(nats_url, connect_timeout=2,
                            max_reconnect_attempts=1, allow_reconnect=False)
    events: list[dict] = []
    try:
        js = nc.jetstream()
        sub = await js.pull_subscribe(">", durable=durable, stream="AGENT_EVENTS")
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


async def _run_fake_task(objective: str) -> dict:
    from adapters.fake.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60
    ) as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "p2", "method": "message/send",
            "params": {"message": {
                "role": "user", "parts": [{"kind": "text", "text": objective}],
            }},
        })
        task = r.json()["result"]
        from .poll import wait_terminal
        return await wait_terminal(c, task["id"])


@requires_nats
async def test_nats_interruption_and_replay(tmp_path, monkeypatch):
    test_port = _free_loopback_port()
    test_url = f"nats://127.0.0.1:{test_port}"
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    store_dir = tmp_path / "jetstream"
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", test_url)

    from orchestrator.nats_client import ensure_stream

    # ── 1. NATS 在线：事件进入 JetStream ──
    proc = _start_nats(store_dir, test_port)
    try:
        _wait_nats_up(test_port)
        await ensure_stream(test_url)
        task1 = await _run_fake_task("phase2: online task")
        assert task1["status"]["state"] == "completed"
        events = await _fetch_all_events("test-consumer-1", test_url)
        types = {e["event_type"] for e in events}
        assert {"task.started", "task.completed", "artifact.created"} <= types
        assert all(e["task_id"] == task1["id"] for e in events)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # ── 2. NATS 停止：事件写入本地 spool ──
    spool = ws / "logs" / "events-pending.jsonl"
    task2 = await _run_fake_task("phase2: offline task")
    assert task2["status"]["state"] == "completed"  # A2A 调用不受 NATS 影响
    # 异步 A2A：终态可见时完成事件的 spool 写入可能仍在进行，稍等其落盘
    spooled: list[dict] = []
    for _ in range(50):
        await asyncio.sleep(0.2)
        if spool.exists():
            spooled = [json.loads(line)
                       for line in spool.read_text().splitlines() if line]
            if {e["event_type"] for e in spooled} >= {"task.started",
                                                      "task.completed"}:
                break
    assert spool.exists()
    assert {e["event_type"] for e in spooled} >= {"task.started", "task.completed"}
    assert all(e["task_id"] == task2["id"] for e in spooled)

    # ── 3. NATS 重启：spool 重放，consumer 可消费 ──
    proc = _start_nats(store_dir, test_port)
    try:
        _wait_nats_up(test_port)
        await ensure_stream(test_url)
        from adapters.common import EventPublisher

        publisher = EventPublisher("replay-test", test_url)
        publisher.spool = spool
        replayed = await publisher.replay_pending()
        assert replayed == len(spooled)
        assert not spool.exists()  # 重放后归档

        events = await _fetch_all_events("test-consumer-2", test_url)
        task2_events = [e for e in events if e["task_id"] == task2["id"]]
        assert {e["event_type"] for e in task2_events} >= {
            "task.started", "task.completed",
        }
    finally:
        proc.terminate()
        proc.wait(timeout=10)
