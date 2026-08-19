"""Phase 2 验收：Codex Adapter 真实任务（设计文档 §20 Phase 2 / §21）。

默认跳过（消耗真实模型调用）；显式开启：
  LAS_RUN_CODEX=1 pytest tests/integration/test_codex_adapter.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

import httpx
import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_CODEX") != "1",
        reason="set LAS_RUN_CODEX=1 to run real codex task",
    ),
    pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed"),
]


async def test_codex_real_task(tmp_path, monkeypatch):
    """§21 验收场景：Create hello.py and add a unit test."""
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")  # 离线 → 走 spool
    monkeypatch.setenv("LAS_ACTION_RECEIPT_SECRET", "c" * 32)

    from adapters.codex.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=600
    ) as c:
        r = await c.get("/.well-known/agent-card.json")
        card = r.json()
        assert card["name"] == "codex"
        skill_ids = {s["id"] for s in card["skills"]}
        assert {"coding", "testing"} <= skill_ids

        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "codex-1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text":
                           "Create hello.py that prints 'hello from codex', "
                           "and add a pytest unit test for it. Run the test."}],
                "metadata": {"idempotencyKey": "T-P2-CODEX:1",
                             "traceId": "trace-phase2-codex"},
            }},
        })
        task = r.json()["result"]
        deadline = time.monotonic() + 600
        approval_seq = 0
        while time.monotonic() < deadline:
            task = (await c.post("/a2a", json={
                "jsonrpc": "2.0", "id": "poll", "method": "tasks/get",
                "params": {"id": task["id"]},
            })).json()["result"]
            state = task["status"]["state"]
            if state in {"completed", "failed", "canceled", "rejected"}:
                break
            if state == "input-required":
                interaction = task["metadata"]["agentHub"][
                    "pendingInteractions"][0]
                hub = task["metadata"]["agentHub"]
                from common.action_receipt import sign_action_receipt

                approval_seq += 1
                receipt = sign_action_receipt({
                    "actionIntentId": f"AI-CODEX-{approval_seq}",
                    "taskId": task["id"],
                    "interactionId": interaction["interactionId"],
                    "nativeRequestId": interaction["nativeRequestId"],
                    "nativeSessionId": interaction["nativeSessionId"],
                    "contextRevision": hub["contextRevision"],
                    "status": "approved", "decidedBy": "user",
                })
                response = await c.post("/a2a", json={
                    "jsonrpc": "2.0", "id": f"approve-{approval_seq}",
                    "method": "extensions/session/interactions/respond",
                    "params": {
                        "id": task["id"],
                        "interactionId": interaction["interactionId"],
                        "respondedBy": "user",
                        "response": {"outcome": "allowed-once",
                                     "authorization": receipt},
                    },
                })
                assert "error" not in response.json(), response.json()
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError("Codex real task did not reach terminal state")

    assert task["status"]["state"] == "completed", task.get("error")
    names = {a["name"] for a in task["artifacts"]}
    assert "codex-app-server.jsonl" in names
    assert task["metadata"]["agentHub"]["nativeSessionId"], task
    # Codex 真实产出的源码文件应被收集为 workspace/* artifact
    produced = [n for n in names if n.startswith("workspace/")]
    assert produced, f"no workspace files produced: {names}"
    # 事件暂存（NATS 离线）
    spool = ws / "logs" / "events-pending.jsonl"
    assert spool.exists() and spool.stat().st_size > 0
