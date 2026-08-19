"""Session Adapter SDK capability and multi-turn behavior tests."""

from __future__ import annotations

import pytest

from adapters.common import A2aTask
from adapters.session import (
    RunnerSessionAdapter,
    SessionCapabilityError,
    SessionMessage,
)

pytestmark = pytest.mark.anyio


async def test_runner_wrapper_truthfully_rejects_second_turn():
    async def runner(task):
        return []

    adapter = RunnerSessionAdapter(runner)
    task = A2aTask("T-1", "submitted", "first")
    await adapter.start_session(task, session_id="S-1", metadata={})
    first = await adapter.send_message(
        "S-1", SessionMessage("M-1", "user", "first"))
    assert first.state == "completed"
    assert adapter.capabilities.multi_turn is False
    assert adapter.capabilities.native_resume is False
    with pytest.raises(SessionCapabilityError, match="second message"):
        await adapter.send_message(
            "S-1", SessionMessage("M-2", "user", "second"))


async def test_fake_session_pause_resume_and_cancel(tmp_path, monkeypatch):
    import adapters.fake.session as fake_module
    from adapters.fake.session import FakeSessionAdapter

    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(fake_module, "FAKE_LATENCY_SECONDS", 0)
    adapter = FakeSessionAdapter()
    task = A2aTask("T-2", "submitted", "first")
    handle = await adapter.start_session(
        task, session_id="S-2", metadata={"interactive": True})
    assert handle.session_id == "S-2"
    first = await adapter.send_message(
        "S-2", SessionMessage("M-1", "user", "design"))
    assert first.state == "input-required"

    paused = await adapter.pause("S-2")
    assert paused.status == "paused"
    with pytest.raises(SessionCapabilityError, match="paused"):
        await adapter.send_message(
            "S-2", SessionMessage("M-2", "user", "implement"))
    resumed = await adapter.resume_session("S-2")
    assert resumed.status == "input-required"
    interrupted = await adapter.interrupt("S-2")
    assert interrupted.status == "paused"
    await adapter.resume_session("S-2")
    completed = await adapter.send_message(
        "S-2", SessionMessage(
            "M-3", "user", "implement",
            metadata={"completeSession": True}))
    assert completed.state == "completed"
    assert completed.artifacts

    task2 = A2aTask("T-3", "submitted", "cancel")
    await adapter.start_session(
        task2, session_id="S-3", metadata={"interactive": True})
    canceled = await adapter.cancel("S-3")
    assert canceled.status == "canceled"
    with pytest.raises(SessionCapabilityError, match="not paused"):
        await adapter.resume_session("S-3")
