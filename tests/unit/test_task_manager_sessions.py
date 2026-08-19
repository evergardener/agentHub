"""TaskManager durable Agent Session binding and recovery tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


class FakeA2aClient:
    def __init__(self, *, native: bool):
        self.native = native
        self.sent: list[dict] = []
        self.controls: list[tuple[str, str]] = []

    async def send_message(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})
        caps = {
            "multi_turn": self.native,
            "resume": self.native,
            "native_resume": self.native,
            "durable_session": self.native,
        }
        return {
            "id": kwargs["task_id"],
            "status": {"state": "completed"},
            "metadata": {"agentHub": {
                "sessionId": kwargs["session_id"],
                "nativeSessionId": (
                    "native-session-1" if self.native else None),
                "capabilities": caps,
                "contextRevision": kwargs["context_revision"],
            }},
        }

    async def get_task(self, task_id):
        raise AssertionError("terminal/native response should not poll")

    async def control_session(self, task_id, operation):
        self.controls.append((task_id, operation))
        return {"id": task_id, "status": {"state": operation}}


def _task_with_collaboration(tm):
    from orchestrator import collaboration_store

    conversation_id = collaboration_store.create_conversation(tm.conn)
    collaboration_id = collaboration_store.create_collaboration(
        tm.conn, conversation_id=conversation_id, objective="session test")
    task_id = tm.create_task(
        "implement session recovery", collaboration_id=collaboration_id)
    return task_id, collaboration_id


async def test_native_binding_is_persisted_and_resumed(tmp_path, monkeypatch):
    from orchestrator import collaboration_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, collaboration_id = _task_with_collaboration(tm)

    call = await tm.delegate_task(task_id, "http://fake", "codex")
    await call
    first = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert first["native_session_id"] == "native-session-1"
    assert first["resume_capability"] == "native"
    assert first["context_revision"] == 1

    call = await tm.delegate_task(task_id, "http://fake", "codex", attempt=2)
    await call
    second = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert second["id"] == first["id"]
    assert second["recovery_state"] == "resumed"
    assert client.sent[1]["native_session_id"] == "native-session-1"
    assert client.sent[1]["metadata"]["recoveryMode"] == "native_resume"
    assert client.sent[1]["replace_session"] is True
    assert client.sent[0]["idempotency_key"] != \
        client.sent[1]["idempotency_key"]
    assert second["last_message_seq"] == 2


async def test_nondurable_binding_creates_audited_replacement(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=False)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)

    await (await tm.delegate_task(task_id, "http://fake", "kimi"))
    first = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "kimi")
    assert first["resume_capability"] == "snapshot"
    await (await tm.delegate_task(
        task_id, "http://fake", "kimi", attempt=2))
    second = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "kimi")
    assert second["id"] != first["id"]
    assert second["replacement_of_id"] == first["id"]
    assert second["recovery_state"] == "replaced"
    assert client.sent[1]["native_session_id"] is None
    assert client.sent[1]["metadata"]["recoveryMode"] == "replacement"


async def test_session_control_updates_binding_phase_and_audit(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, collaboration_id = _task_with_collaboration(tm)
    await (await tm.delegate_task(task_id, "http://fake", "codex"))

    await tm.control_agent_session(
        task_id, agent_id="codex", endpoint="http://fake",
        operation="pause", requested_by="user")
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert binding["status"] == "paused"
    assert collaboration_store.get_collaboration(
        tm.conn, collaboration_id)["phase"] == "paused"

    await tm.control_agent_session(
        task_id, agent_id="codex", endpoint="http://fake",
        operation="resume", requested_by="user")
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert binding["status"] == "active"
    assert client.controls == [(task_id, "pause"), (task_id, "resume")]
    event_types = [r["event_type"] for r in tm.conn.execute(
        "SELECT event_type FROM events ORDER BY seq;").fetchall()]
    assert "agent.session.pause" in event_types
    assert "agent.session.resume" in event_types


async def test_blocked_recovery_does_not_mutate_queued_task(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store, state_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=False)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, collaboration_id = _task_with_collaboration(tm)
    collaboration_store.upsert_agent_session(
        tm.conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="kimi", adapter_session_id="lost-session",
        capabilities={})

    with pytest.raises(RuntimeError, match="safe session recovery unavailable"):
        await tm.delegate_task(task_id, "http://fake", "kimi")
    assert state_store.get_task(tm.conn, task_id)["status"] == "queued"
    assert not client.sent


async def test_tracking_failure_after_dispatch_does_not_fail_task(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store, state_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=False)

    async def send_working(text, **kwargs):
        client.sent.append({"text": text, **kwargs})
        return {
            "id": kwargs["task_id"], "status": {"state": "working"},
            "metadata": {"agentHub": {
                "sessionId": kwargs["session_id"],
                "nativeSessionId": None,
                "capabilities": {"durable_session": False},
                "contextRevision": kwargs["context_revision"],
            }},
        }

    async def fail_poll(task_id):
        raise ConnectionError("poll unavailable")

    client.send_message = send_working
    client.get_task = fail_poll
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)
    await (await tm.delegate_task(task_id, "http://fake", "kimi"))

    assert state_store.get_task(tm.conn, task_id)["status"] == "assigned"
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "kimi")
    assert binding["status"] == "active"
    assert binding["recovery_state"] == "tracking_failed"
