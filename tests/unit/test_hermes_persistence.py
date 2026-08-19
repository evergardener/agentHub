"""Hermes chat persists and restores one collaboration context."""

from __future__ import annotations

import pytest

from hermes.brain import Hermes
from hermes.llm import LLMReply
from orchestrator import collaboration_store
from orchestrator.task_manager import TaskManager


pytestmark = pytest.mark.anyio


class RecordingLLM:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.calls.append(list(messages))
        return LLMReply(
            content=self.answer,
            raw_message={"role": "assistant", "content": self.answer})


async def test_hermes_restores_persisted_collaboration(tmp_path):
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    first_llm = RecordingLLM("已建立任务上下文")
    first = Hermes(tm, llm=first_llm)

    assert await first.chat("上午开始实现持久会话") == "已建立任务上下文"
    conversation_id = first.conversation_id
    collaboration_id = first.collaboration_id
    assert conversation_id and collaboration_id

    second_llm = RecordingLLM("继续昨天的工作")
    resumed = Hermes(
        tm, llm=second_llm, conversation_id=conversation_id,
        collaboration_id=collaboration_id)
    assert await resumed.chat("第二天继续进度") == "继续昨天的工作"

    sent = second_llm.calls[0]
    assert any(m.get("content") == "上午开始实现持久会话" for m in sent)
    assert any(m.get("content") == "已建立任务上下文" for m in sent)
    assert sent[-1]["content"] == "第二天继续进度"

    rows = collaboration_store.list_collaboration_messages(
        tm.conn, collaboration_id)
    assert [r["message_type"] for r in rows] == [
        "llm.user", "llm.assistant", "llm.user", "llm.assistant"]


async def test_hermes_created_tasks_link_to_collaboration(tmp_path):
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    llm = RecordingLLM("ok")
    brain = Hermes(tm, llm=llm)
    await brain.chat("调研消息模型")

    result = await brain.tools.dispatch(
        "create_task", {"objective": "调研数据库消息顺序"})
    row = tm.conn.execute(
        "SELECT collaboration_id FROM tasks WHERE id = ?;",
        (result["task_id"],)).fetchone()
    assert row["collaboration_id"] == brain.collaboration_id


async def test_live_hermes_sees_webui_user_intervention(tmp_path):
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    llm = RecordingLLM("开始")
    brain = Hermes(tm, llm=llm)
    await brain.chat("开始开发")
    collaboration_store.record_user_intervention(
        tm.conn, collaboration_id=brain.collaboration_id,
        user_id="user", mode="steer",
        content={"text": "不要修改数据库"},
        idempotency_key="webui-steer-1")

    await brain.chat("现在进度如何")
    second_call = llm.calls[-1]
    assert any(
        m.get("role") == "user"
        and "用户直接介入子 Agent：steer" in m.get("content", "")
        and "不要修改数据库" in m.get("content", "")
        for m in second_call)
