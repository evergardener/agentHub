"""v3 M1 验收：异步 A2A —— 长任务不占用 HTTP 连接。

模拟一个 8 秒的"长任务"（机制与 20 分钟相同）：
  1. message/send 必须在 1 秒内返回（非终态）
  2. 任务在后台继续执行
  3. tasks/get 轮询最终到达 completed，artifact 完整
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.anyio

SLOW_SECONDS = 8.0


async def test_long_task_does_not_block_http(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")  # 离线 → spool

    from adapters.common import A2aTask, save_artifact
    from adapters.server_common import build_app

    async def slow_runner(task: A2aTask) -> list[dict]:
        await asyncio.sleep(SLOW_SECONDS)
        return [save_artifact(task.id, "slow.md", b"slow done", "report")]

    def card(base_url: str) -> dict:
        return {"name": "slow-worker", "url": base_url, "version": "0.1",
                "capabilities": {}, "skills": [{"id": "slow"}]}

    app = build_app("slow", card, slow_runner)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=10
    ) as c:
        start = time.monotonic()
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "s1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "long running task"}],
                "metadata": {"idempotencyKey": "T-ASYNC:1"},
            }},
        })
        send_elapsed = time.monotonic() - start
        task = r.json()["result"]

        # 关键验收：send 秒级返回，不等任务完成
        assert send_elapsed < 1.0, f"send blocked {send_elapsed:.2f}s"
        assert task["status"]["state"] in ("submitted", "working")

        # 任务在后台推进；轮询到终态
        from .poll import wait_terminal
        final = await wait_terminal(c, task["id"], timeout=30)

    assert final["status"]["state"] == "completed"
    assert final["artifacts"][0]["name"] == "slow.md"
    content = Path(final["artifacts"][0]["path"]).read_text()
    assert content == "slow done"
