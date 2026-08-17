"""§21 第一个完整协作测试（真实 LLM，门控）。

场景：分析某个本地项目的问题并完成修复。
  T001 (kimi)  : inspect project —— 分析 buggy 源码，输出根因
  T002 (codex) : depends_on T001 —— 在工作区写出修复后的文件
  Hermes       : review T001 → accept → 自动解锁 T002 → review T002

开启：LAS_RUN_LLM=1 LAS_RUN_CODEX=1 pytest tests/integration/test_full_collaboration.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_LLM") != "1" or os.environ.get("LAS_RUN_CODEX") != "1",
        reason="set LAS_RUN_LLM=1 LAS_RUN_CODEX=1 to run full collaboration",
    ),
]

BUGGY_SRC = '''\
def divide(a, b):
    # 期望：a 除以 b
    return a * b


if __name__ == "__main__":
    assert divide(10, 2) == 5, "divide broken"
    print("OK")
'''


async def _run_on(app_factory, objective: str, task_id: str) -> dict:
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


async def test_analyze_and_fix_project(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(ws))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")  # 离线 → spool

    from orchestrator.task_manager import TaskManager
    from orchestrator import state_store
    from common.models import TaskStatus
    import adapters.kimi.server as kimi_server
    import adapters.codex.server as codex_server

    tm = TaskManager(db_path=tmp_path / "state.db", workspace=ws)

    # T001: inspect project（kimi 分析根因）
    t1 = tm.create_task("分析 calc.py 的 bug 根因")
    r1 = await _run_on(
        kimi_server.create_app,
        "分析以下 Python 源码的 bug 根因，一句话说明：\n\n" + BUGGY_SRC, t1)
    assert r1["status"]["state"] == "completed", r1.get("error")
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
        try:
            state_store.transition_task(tm.conn, t1, dst)
        except Exception:
            pass
    assert tm.review_result(t1, approved=True) == "accepted"

    # T002: codex 修复（depends_on T001，验收后自动解锁）
    analysis = Path(r1["artifacts"][0]["path"]).read_text(encoding="utf-8")
    t2 = tm.create_task("修复 calc.py", depends_on=[t1])
    assert state_store.get_task(tm.conn, t2)["status"] == "queued"
    r2 = await _run_on(
        codex_server.create_app,
        "在工作区创建 calc.py，修复下面代码的 bug（分析结论附后），"
        "使得运行 python3 calc.py 输出 OK。\n\n源码：\n" + BUGGY_SRC
        + "\n\n分析结论：\n" + analysis[:800], t2)
    assert r2["status"]["state"] == "completed", r2.get("error")

    # 验证修复产物真实可运行
    fixed = None
    for a in r2["artifacts"]:
        if a["name"].endswith("calc.py"):
            fixed = Path(a["path"])
            break
    assert fixed is not None, f"no calc.py artifact: {[a['name'] for a in r2['artifacts']]}"
    proc = subprocess.run(["python3", str(fixed)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr
