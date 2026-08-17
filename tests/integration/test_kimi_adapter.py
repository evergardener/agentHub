"""Phase 6 验收：Kimi Adapter 真实 LLM 任务 + 多 Worker 协作链。

默认跳过（真实 LLM 调用）；显式开启：
  LAS_RUN_LLM=1 pytest tests/integration/test_kimi_adapter.py
  LAS_RUN_LLM=1 LAS_RUN_CODEX=1 ...  # 含 codex 实现腿的完整链
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_LLM") != "1",
        reason="set LAS_RUN_LLM=1 to run real LLM task",
    ),
]


async def test_kimi_research_task(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")  # 离线 → spool

    from adapters.kimi.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=300
    ) as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
        assert card["name"] == "kimi"
        assert {"research", "long_context"} <= {s["id"] for s in card["skills"]}

        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "kimi-1", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text":
                           "调研：本地多 Agent 系统中，SQLite 作为唯一事实源的优缺点，"
                           "三条以内。"}],
                "metadata": {"idempotencyKey": "T-P6-KIMI:1"},
            }},
        })
        task = r.json()["result"]

    assert task["status"]["state"] == "completed", task.get("error")
    artifact = task["artifacts"][0]
    assert artifact["name"] == "analysis.md"
    content = Path(artifact["path"]).read_text(encoding="utf-8")
    assert len(content) > 100  # 真实分析内容


@pytest.mark.skipif(
    os.environ.get("LAS_RUN_CODEX") != "1",
    reason="set LAS_RUN_CODEX=1 for full kimi->codex chain",
)
async def test_kimi_then_codex_chain(tmp_path, monkeypatch):
    """§20 Phase 6 验收：Kimi 调研 → Codex 实现（depends_on 串联）。"""
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")

    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store
    from common.models import TaskStatus
    import adapters.kimi.server as kimi_server
    import adapters.codex.server as codex_server

    tm = TaskManager(db_path=tmp_path / "state.db", workspace=ws)

    async def run_on(app_factory, objective, task_id):
        transport = httpx.ASGITransport(app=app_factory())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=600
        ) as c:
            r = await c.post("/a2a", json={
                "jsonrpc": "2.0", "id": task_id, "method": "message/send",
                "params": {"message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": objective}],
                    "metadata": {"taskId": task_id,
                                 "idempotencyKey": f"{task_id}:1"},
                }},
            })
            return r.json()["result"]

    # T1: Kimi 调研
    t1 = tm.create_task("用一句话说明 Python 的 GIL 是什么")
    t1_result = await run_on(kimi_server.create_app,
                             "用一句话说明 Python 的 GIL 是什么", t1)
    assert t1_result["status"]["state"] == "completed"
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
        try:
            state_store.transition_task(tm.conn, t1, dst)
        except Exception:
            pass
    tm.review_result(t1, approved=True)

    # T2: Codex 实现，depends_on T1 —— 验收后自动解锁
    t2 = tm.create_task("Create gil_demo.py demonstrating threads and the GIL",
                        depends_on=[t1])
    assert state_store.get_task(tm.conn, t2)["status"] == "queued"
    t2_result = await run_on(codex_server.create_app,
                             "Create gil_demo.py with a short comment explaining"
                             " the GIL, based on prior research.", t2)
    assert t2_result["status"]["state"] == "completed", t2_result.get("error")
