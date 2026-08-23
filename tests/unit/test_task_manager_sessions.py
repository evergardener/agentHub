"""TaskManager durable Agent Session binding and recovery tests."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.anyio


class FakeA2aClient:
    def __init__(self, *, native: bool):
        self.native = native
        self.sent: list[dict] = []
        self.controls: list[tuple[str, str]] = []
        self.steers: list[dict] = []

    async def send_message(self, text, **kwargs):
        self.sent.append({"text": text, **kwargs})
        caps = {
            "multi_turn": self.native,
            "resume": self.native,
            "native_resume": self.native,
            "durable_session": self.native,
            "steer": self.native,
            "interrupt": self.native,
            "cancel": True,
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

    async def steer_session(self, task_id, text, *, context_revision,
                            message_id=None):
        self.steers.append({
            "task_id": task_id, "text": text,
            "context_revision": context_revision, "message_id": message_id,
        })
        return {"id": task_id, "status": {"state": "working"}}


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


async def test_rework_continuation_reuses_native_session_and_new_attempt(
        tmp_path, monkeypatch):
    from common.models import TaskStatus
    from orchestrator import collaboration_store, state_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)

    first_call = await tm.delegate_task(task_id, "http://fake", "codex")
    await first_call
    first = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    state_store.transition_task(tm.conn, task_id, TaskStatus.WORKING)
    state_store.transition_task(
        tm.conn, task_id, TaskStatus.AWAITING_ACCEPTANCE)
    tm.reject_result(task_id, feedback="please fix tests")

    second_call = await tm.delegate_task(task_id, "http://fake", "codex")
    await second_call
    second = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")

    assert state_store.get_task(tm.conn, task_id)["status"] == "assigned"
    assert second["id"] == first["id"]
    assert client.sent[1]["native_session_id"] == "native-session-1"
    assert client.sent[1]["metadata"]["attempt"] == 2
    event = tm.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.rework.dispatched' ORDER BY seq DESC LIMIT 1;",
        (task_id,),
    ).fetchone()
    assert json.loads(event["payload_json"])["attempt"] == 2


async def test_structured_plan_contract_is_sent_and_persisted(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    conversation_id = collaboration_store.create_conversation(tm.conn)
    collaboration_id = collaboration_store.create_collaboration(
        tm.conn, conversation_id=conversation_id, objective="planned")
    contract = {
        "step_key": "backend", "profile_id": "AP-CODEX-BACKEND",
        "expected_operations": ["filesystem.write"],
        "acceptance_criteria": ["tests pass"],
    }
    task_id = tm.create_task(
        "implement API", collaboration_id=collaboration_id, context=contract)
    await (await tm.delegate_task(task_id, "http://fake", "codex"))

    sent = client.sent[0]
    assert "AgentHub structured Task Plan contract" in sent["text"]
    assert "ActionIntent" in sent["text"]
    assert sent["metadata"]["taskPlan"] == contract
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    snapshot = json.loads(binding["context_snapshot_json"])
    assert snapshot["task_plan"] == contract


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


async def test_user_steer_advances_context_and_reaches_same_session(
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

    result = await tm.intervene_agent_session(
        task_id, mode="steer", content={"text": "不要改数据库"},
        agent_id="codex", endpoint="http://fake", user_id="user",
        idempotency_key="steer-once")

    assert result["context_revision"] == 2
    assert client.steers == [{
        "task_id": task_id, "text": "不要改数据库",
        "context_revision": 2, "message_id": result["message_id"],
    }]
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert binding["context_revision"] == 2
    collaboration = collaboration_store.get_collaboration(
        tm.conn, collaboration_id)
    assert collaboration["phase"] == "executing"
    assert collaboration["controller"] == "user"
    duplicate = await tm.intervene_agent_session(
        task_id, mode="steer", content={"text": "不要改数据库"},
        agent_id="codex", endpoint="http://fake", user_id="user",
        idempotency_key="steer-once")
    assert duplicate["duplicate"] is True
    assert len(client.steers) == 1


async def test_disabled_agent_cannot_receive_user_steer(
        tmp_path, monkeypatch):
    from orchestrator import agent_control_store
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)
    await (await tm.delegate_task(task_id, "http://fake", "codex"))
    agent_control_store.set_enabled(
        tm.conn, agent_id="codex", enabled=False, updated_by="test")

    with pytest.raises(PermissionError, match="agent is disabled: codex"):
        await tm.intervene_agent_session(
            task_id, mode="steer", content={"text": "@codex 继续检查"},
            agent_id="codex", endpoint="http://fake", user_id="user",
            idempotency_key="disabled-steer")

    assert client.steers == []
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
        " WHERE idempotency_key = 'disabled-steer';"
    ).fetchone()[0] == 0


async def test_failed_steer_can_retry_same_idempotency_key(
        tmp_path, monkeypatch):
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=True)
    attempts = 0
    original = client.steer_session

    async def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("adapter unavailable")
        return await original(*args, **kwargs)

    client.steer_session = flaky
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)
    await (await tm.delegate_task(task_id, "http://fake", "codex"))
    kwargs = dict(
        mode="steer", content={"text": "只改 API"}, agent_id="codex",
        endpoint="http://fake", idempotency_key="retry-steer")
    with pytest.raises(ConnectionError, match="unavailable"):
        await tm.intervene_agent_session(task_id, **kwargs)
    result = await tm.intervene_agent_session(task_id, **kwargs)
    assert result["context_revision"] == 2
    assert attempts == 2


async def test_user_takeover_interrupts_then_returns_control_to_hermes(
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

    with pytest.raises(ValueError, match="not under user control"):
        await tm.intervene_agent_session(
            task_id, mode="return_to_hermes",
            content={"text": "尚未接管"}, agent_id="codex",
            idempotency_key="premature-return")
    assert tm.conn.execute(
        "SELECT context_revision FROM collaborations WHERE id = ?;",
        (collaboration_id,),
    ).fetchone()[0] == 1

    takeover = await tm.intervene_agent_session(
        task_id, mode="takeover", content={"text": "停止修改数据库"},
        agent_id="codex", endpoint="http://fake", user_id="user",
        idempotency_key="takeover-once")
    assert takeover["context_revision"] == 2
    assert client.controls == [(task_id, "interrupt")]
    binding = collaboration_store.get_current_agent_session(
        tm.conn, task_id, "codex")
    assert binding["status"] == "paused"
    assert binding["context_revision"] == 2
    collaboration = collaboration_store.get_collaboration(
        tm.conn, collaboration_id)
    assert collaboration["phase"] == "needs_replan"
    assert collaboration["controller"] == "user"
    takeover_message = tm.conn.execute(
        "SELECT recipient_type, recipient_id FROM conversation_messages"
        " WHERE id = ?;", (takeover["message_id"],),
    ).fetchone()
    assert (takeover_message["recipient_type"],
            takeover_message["recipient_id"]) == ("hermes", "hermes")

    duplicate = await tm.intervene_agent_session(
        task_id, mode="takeover", content={"text": "停止修改数据库"},
        agent_id="codex", endpoint="http://fake", user_id="user",
        idempotency_key="takeover-once")
    assert duplicate["duplicate"] is True
    assert client.controls == [(task_id, "interrupt")]

    returned = await tm.intervene_agent_session(
        task_id, mode="return_to_hermes",
        content={"text": "按最新约束重新规划"}, agent_id="codex",
        user_id="user", idempotency_key="return-once")
    assert returned["context_revision"] == 3
    collaboration = collaboration_store.get_collaboration(
        tm.conn, collaboration_id)
    assert collaboration["phase"] == "needs_replan"
    assert collaboration["controller"] == "hermes"
    returned_message = tm.conn.execute(
        "SELECT recipient_type, recipient_id FROM conversation_messages"
        " WHERE id = ?;", (returned["message_id"],),
    ).fetchone()
    assert (returned_message["recipient_type"],
            returned_message["recipient_id"]) == ("hermes", "hermes")
    returned_duplicate = await tm.intervene_agent_session(
        task_id, mode="return_to_hermes",
        content={"text": "按最新约束重新规划"}, agent_id="codex",
        user_id="user", idempotency_key="return-once")
    assert returned_duplicate["duplicate"] is True


async def test_takeover_requires_native_interrupt_capability(
        tmp_path, monkeypatch):
    from orchestrator.a2a_client import A2aClient
    from orchestrator.task_manager import TaskManager

    client = FakeA2aClient(native=False)
    monkeypatch.setattr(
        A2aClient, "for_agent", classmethod(
            lambda cls, agent_name, direct_endpoint, timeout=30: client))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    task_id, _ = _task_with_collaboration(tm)
    await (await tm.delegate_task(task_id, "http://fake", "kimi"))

    with pytest.raises(ValueError, match="safely interrupted"):
        await tm.intervene_agent_session(
            task_id, mode="takeover", content={"text": "用户接管"},
            agent_id="kimi", endpoint="http://fake",
            idempotency_key="unsafe-takeover")
    assert tm.conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
        " WHERE idempotency_key = 'unsafe-takeover';"
    ).fetchone()[0] == 0


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
