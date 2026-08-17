"""Fake Worker 运行时 — 设计文档 §27 Task 4。

receive task → sleep 1s → create artifact → publish completed → return result。
不接真实 Codex（Phase 2 替换本模块）。
"""

from __future__ import annotations

import asyncio

from adapters.common import A2aTask, save_artifact

FAKE_LATENCY_SECONDS = 1.0


async def run(task: A2aTask) -> list[dict]:
    """执行假任务，返回 artifact 列表。"""
    await asyncio.sleep(FAKE_LATENCY_SECONDS)
    content = (
        f"# Fake Worker Result\n\n"
        f"- task_id: {task.id}\n"
        f"- objective: {task.objective}\n"
        f"- status: simulated success\n"
    ).encode("utf-8")
    artifact = save_artifact(task.id, "result.md", content, artifact_type="report")
    return [artifact]
