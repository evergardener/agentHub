"""A2A Adapter 契约测试 — 设计文档 §25 契约测试。

与具体 Agent 无关的行为规范。同一套测试必须能跑任何 Adapter：
  - Fake Worker（当前）
  - Codex Adapter（Phase 2）
  - Kimi Adapter（Phase 6）

用法：设置 CONTRACT_BASE_URL 指向被测 Adapter；
未设置时默认拉起内存中的 fake worker（httpx ASGI transport）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.anyio

A2A_STATES = {
    "submitted", "working", "input-required",
    "completed", "failed", "canceled", "rejected", "unknown",
}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "logs").mkdir()
    os.environ["AGENT_WORKSPACE"] = str(tmp_path)
    return tmp_path


@pytest.fixture
async def client(workspace):
    base_url = os.environ.get("CONTRACT_BASE_URL")
    if base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as c:
            yield c
    else:
        from adapters.fake.server import create_app

        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            yield c


async def _send(client: httpx.AsyncClient, text: str, rpc_id: str = "1",
                metadata: dict | None = None) -> dict:
    r = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": rpc_id, "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "metadata": metadata or {},
        }},
    })
    assert r.status_code == 200
    return r.json()


async def _wait_terminal(client: httpx.AsyncClient, task_id: str,
                         timeout: float = 30.0) -> dict:
    """异步 A2A（v3 M1）：轮询 tasks/get 直到终态。"""
    import time

    terminal = {"completed", "failed", "canceled", "rejected"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "poll", "method": "tasks/get",
            "params": {"id": task_id},
        })
        task = r.json()["result"]
        if task["status"]["state"] in terminal:
            return task
        await asyncio.sleep(0.2)
    raise TimeoutError(f"task {task_id} not terminal within {timeout}s")


# ---------- 契约条款 ----------


async def test_agent_card_shape(client):
    r = await client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    for field in ("name", "url", "version", "capabilities", "skills"):
        assert field in card, f"agent card missing {field}"
    assert isinstance(card["skills"], list) and card["skills"], "skills must be non-empty"


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_task_completes_with_artifacts(client, workspace):
    resp = await _send(client, "contract: basic task", rpc_id="c1")
    task = resp["result"]
    # 异步 A2A：send 立即返回非终态，终态经轮询到达
    assert task["status"]["state"] in A2A_STATES
    task = await _wait_terminal(client, task["id"])
    assert task["status"]["state"] == "completed"
    assert task["artifacts"], "completed task must produce artifacts"
    # Artifact 完整性：sha256 可验证（§22.4）
    for a in task["artifacts"]:
        path = Path(a["path"])
        assert path.exists(), f"artifact missing: {path}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == a["sha256"]


async def test_tasks_get_roundtrip(client):
    resp = await _send(client, "contract: get task", rpc_id="c2")
    task_id = resp["result"]["id"]
    r = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "c3", "method": "tasks/get",
        "params": {"id": task_id},
    })
    assert r.json()["result"]["id"] == task_id


async def test_idempotency_key_dedup(client):
    key = "contract-idem-0001"
    r1 = await _send(client, "contract: idem", rpc_id="c4",
                     metadata={"idempotencyKey": key})
    r2 = await _send(client, "contract: idem", rpc_id="c5",
                     metadata={"idempotencyKey": key})
    assert r1["result"]["id"] == r2["result"]["id"], \
        "same idempotency key must return same task (§22.5)"


async def test_empty_message_rejected(client):
    resp = await _send(client, "", rpc_id="c6")
    assert "error" in resp


async def test_unknown_method_rejected(client):
    r = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "c7", "method": "bogus/method", "params": {},
    })
    assert "error" in r.json()
    assert r.json()["error"]["code"] == -32601


async def test_fifo_serial_execution(client):
    """单并发 Adapter：两个任务全部完成的总耗时 >= 2 倍单任务时延（§9.1）。

    异步 A2A 下 send 都立即返回，串行性体现在终态到达的总时长上。
    """
    import time

    start = time.monotonic()
    r1, r2 = await asyncio.gather(
        _send(client, "contract: fifo-1", rpc_id="c8"),
        _send(client, "contract: fifo-2", rpc_id="c9"),
    )
    send_elapsed = time.monotonic() - start
    assert send_elapsed < 1.0, f"send must return immediately, took {send_elapsed:.2f}s"
    t1, t2 = await asyncio.gather(
        _wait_terminal(client, r1["result"]["id"]),
        _wait_terminal(client, r2["result"]["id"]),
    )
    elapsed = time.monotonic() - start
    assert t1["status"]["state"] == "completed"
    assert t2["status"]["state"] == "completed"
    assert elapsed >= 1.8, f"expected serial execution (>=2s), got {elapsed:.2f}s"
