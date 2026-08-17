"""Phase 1 验收场景 — 设计文档 §20 Phase 1。

Hermes 发出 "Create hello.py and add a unit test."，
Fake Worker 返回结构化 Artifact，Hermes 可以读取结果。

真实 HTTP 链路：uvicorn 起在 127.0.0.1:8299，A2aClient 正常走网络。
"""

from __future__ import annotations

import os
import threading
import time

import httpx
import pytest
import uvicorn

from orchestrator.a2a_client import A2aClient

pytestmark = pytest.mark.anyio

PORT = 8299
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    ws = tmp_path_factory.mktemp("agent-workspace-e2e")
    (ws / "logs").mkdir()
    os.environ["AGENT_WORKSPACE"] = str(ws)

    from adapters.fake.server import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=PORT,
                            log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        raise RuntimeError("fake worker did not start")
    yield ws
    srv.should_exit = True
    thread.join(timeout=5)


async def test_phase1_acceptance(server):
    """§20 Phase 1 验收：委派 → 执行 → Artifact → Hermes 读取结果。"""
    client = A2aClient(BASE)

    # 1. 能力发现
    card = await client.get_agent_card()
    assert card["name"] == "fake-worker"

    # 2. Hermes 委派任务（带幂等键与 trace_id）；异步 A2A：轮询到终态
    task = await client.send_and_wait(
        "Create hello.py and add a unit test.",
        idempotency_key="T-P1-ACCEPTANCE:1",
        trace_id="trace-phase1-acceptance",
    )
    assert task["status"]["state"] == "completed"

    # 3. Artifact 结构化返回
    assert task["artifacts"], "no artifacts returned"
    artifact = task["artifacts"][0]
    assert artifact["name"] == "result.md"
    assert artifact["task_id"] == task["id"]

    # 4. Hermes 读取结果内容
    from pathlib import Path

    content = Path(artifact["path"]).read_text(encoding="utf-8")
    assert "Create hello.py" in content

    # 5. 任务可回查
    fetched = await client.get_task(task["id"])
    assert fetched["status"]["state"] == "completed"

    # 6. 幂等重放不产生新任务
    replay = await client.send_message(
        "Create hello.py and add a unit test.",
        idempotency_key="T-P1-ACCEPTANCE:1",
    )
    assert replay["id"] == task["id"]
