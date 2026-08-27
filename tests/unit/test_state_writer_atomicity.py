"""Fault injection proves event dedupe and state mutation commit atomically."""

from __future__ import annotations

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from state.writer import (
    MAX_CONVERSATION_RESULT_CHARS,
    StateWriter,
    _bounded_conversation_result,
)


def test_conversation_result_has_a_fail_closed_size_limit():
    text = "长" * (MAX_CONVERSATION_RESULT_CHARS + 1)
    result = _bounded_conversation_result(text)
    assert len(result) <= MAX_CONVERSATION_RESULT_CHARS
    assert result.startswith("长" * (MAX_CONVERSATION_RESULT_CHARS - 100))
    assert "State Writer 已按安全上限截断" in result


def test_worker_completed_event_enters_awaiting_acceptance(tmp_path):
    from orchestrator import collaboration_store

    writer = StateWriter(tmp_path / "acceptance.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="implement",
    )
    state_store.create_task(
        writer.conn, task_id="T-accept", objective="implement",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(writer.conn, "T-accept", TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, "T-accept", TaskStatus.WORKING)

    assert writer.apply({
        "event_id": "E-completed-acceptance",
        "event_type": "task.completed",
        "task_id": "T-accept",
        "source": "codex",
        "payload": {"attempt": 1, "summary": "done",
                    "result_text": "done\n" + "完整结果" * 3000},
    }) == "applied"
    task = state_store.get_task(writer.conn, "T-accept")
    assert task["status"] == "awaiting_acceptance"
    collaboration = collaboration_store.get_collaboration(
        writer.conn, collaboration_id)
    assert collaboration["phase"] == "awaiting_acceptance"
    run = writer.conn.execute(
        "SELECT attempt, status FROM task_runs WHERE task_id = 'T-accept'"
        " ORDER BY started_at DESC LIMIT 1;").fetchone()
    assert (run["attempt"], run["status"]) == (1, "completed")
    message = writer.conn.execute(
        "SELECT sender_type, sender_id, message_type, content_json"
        " FROM conversation_messages WHERE task_id = 'T-accept';"
    ).fetchone()
    assert message["sender_type"] == "agent"
    assert message["sender_id"] == "codex"
    assert message["message_type"] == "agent.task.result"
    assert '"text":"done\\n完整结果完整结果' in message["content_json"]
    assert state_store.get_task(
        writer.conn, "T-accept")["result_summary"] == "done"

    assert writer.apply({
        "event_id": "E-completed-acceptance",
        "event_type": "task.completed",
        "task_id": "T-accept",
        "source": "codex",
        "payload": {"attempt": 1, "summary": "done",
                    "result_text": "done\n" + "完整结果" * 3000},
    }) == "duplicate"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
        " WHERE task_id = 'T-accept';"
    ).fetchone()[0] == 1


def test_result_message_failure_rolls_back_completed_event(
        tmp_path, monkeypatch):
    from orchestrator import collaboration_store

    writer = StateWriter(tmp_path / "result-atomicity.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="atomic result",
    )
    state_store.create_task(
        writer.conn, task_id="T-result-atomic", objective="atomic result",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(
        writer.conn, "T-result-atomic", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-result-atomic", TaskStatus.WORKING)

    def fail(*args, **kwargs):
        raise OSError("injected result message failure")

    monkeypatch.setattr(collaboration_store, "append_message", fail)
    event = {
        "event_id": "E-result-atomic",
        "event_type": "task.completed",
        "task_id": "T-result-atomic",
        "source": "codex",
        "payload": {"attempt": 1, "summary": "done atomically"},
    }
    with pytest.raises(OSError, match="result message failure"):
        writer.apply(event)
    assert state_store.get_task(
        writer.conn, "T-result-atomic")["status"] == "working"
    assert collaboration_store.get_collaboration(
        writer.conn, collaboration_id)["phase"] == "planning"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'E-result-atomic';"
    ).fetchone()[0] == 0
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = 'T-result-atomic';"
    ).fetchone()[0] == 0


def test_legacy_completed_event_without_result_text_uses_summary(tmp_path):
    from orchestrator import collaboration_store

    writer = StateWriter(tmp_path / "legacy-result.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="legacy result",
    )
    state_store.create_task(
        writer.conn, task_id="T-legacy-result", objective="legacy result",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(
        writer.conn, "T-legacy-result", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-legacy-result", TaskStatus.WORKING)

    assert writer.apply({
        "event_id": "E-legacy-result",
        "event_type": "task.completed",
        "task_id": "T-legacy-result",
        "source": "codex",
        "payload": {"attempt": 1, "summary": "legacy summary"},
    }) == "applied"
    message = writer.conn.execute(
        "SELECT content_json FROM conversation_messages"
        " WHERE task_id = 'T-legacy-result';"
    ).fetchone()
    assert '"text":"legacy summary"' in message["content_json"]


def _event(event_id: str, event_type: str, task_id: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "task_id": task_id,
        "source": "codex",
        "payload": {"attempt": 1},
    }


def _assigned_task(writer: StateWriter, task_id: str) -> None:
    state_store.create_task(
        writer.conn, task_id=task_id, objective="fault injection",
        created_by="hermes", status=TaskStatus.QUEUED)
    state_store.transition_task(writer.conn, task_id, TaskStatus.ASSIGNED)


def test_transition_failure_rolls_back_dedupe_record_for_redelivery(
        tmp_path, monkeypatch):
    writer = StateWriter(tmp_path / "writer.db")
    _assigned_task(writer, "T-atomic-1")
    original = state_store.transition_task
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected database interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(state_store, "transition_task", fail_once)
    event = _event("E-atomic-1", "task.started", "T-atomic-1")
    with pytest.raises(OSError, match="injected"):
        writer.apply(event)
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'E-atomic-1';"
    ).fetchone()[0] == 0
    assert state_store.get_task(writer.conn, "T-atomic-1")["status"] == "assigned"

    assert writer.apply(event) == "applied"
    assert state_store.get_task(writer.conn, "T-atomic-1")["status"] == "working"
    assert writer.apply(event) == "duplicate"


def test_run_insert_failure_rolls_back_transition_and_event(tmp_path, monkeypatch):
    writer = StateWriter(tmp_path / "writer.db")
    _assigned_task(writer, "T-atomic-2")
    original = state_store.add_task_run
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected run write interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(state_store, "add_task_run", fail_once)
    event = _event("E-atomic-2", "task.started", "T-atomic-2")
    with pytest.raises(OSError, match="injected"):
        writer.apply(event)
    assert state_store.get_task(writer.conn, "T-atomic-2")["status"] == "assigned"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'E-atomic-2';"
    ).fetchone()[0] == 0
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = 'T-atomic-2';"
    ).fetchone()[0] == 0

    assert writer.apply(event) == "applied"
    assert state_store.get_task(writer.conn, "T-atomic-2")["status"] == "working"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = 'T-atomic-2';"
    ).fetchone()[0] == 1


def test_connection_failure_reconnects_but_preserves_nak_signal(
        tmp_path, monkeypatch):
    writer = StateWriter(tmp_path / "writer.db")
    replacement = object()
    closed = []

    class OldConnection:
        def close(self):
            closed.append(True)

    writer.conn = OldConnection()
    monkeypatch.setattr("state.writer.init_db", lambda target: replacement)
    failure = ConnectionError("postgres connection dropped")
    monkeypatch.setattr(writer, "apply", lambda event: (_ for _ in ()).throw(failure))

    with pytest.raises(ConnectionError, match="dropped"):
        writer.apply_resilient({"event_id": "E-reconnect"})
    assert writer.conn is replacement
    assert closed == [True]


def test_non_connection_failure_does_not_hide_bug_or_reconnect(
        tmp_path, monkeypatch):
    writer = StateWriter(tmp_path / "writer.db")
    reconnects = []
    monkeypatch.setattr(writer, "reconnect", lambda: reconnects.append(True))
    monkeypatch.setattr(
        writer, "apply",
        lambda event: (_ for _ in ()).throw(ValueError("bad event handler")))
    with pytest.raises(ValueError, match="bad event"):
        writer.apply_resilient({"event_id": "E-bug"})
    assert reconnects == []


def test_psycopg_operational_subclasses_are_connection_failures(tmp_path):
    import psycopg

    writer = StateWriter(tmp_path / "writer.db")
    assert writer._is_connection_failure(
        psycopg.errors.AdminShutdown("server restarting"))


def test_disabled_agent_heartbeat_is_audited_but_not_registered(tmp_path):
    agents_path = tmp_path / "agents.yaml"
    agents_path.write_text(
        "agents:\n"
        "  codex:\n"
        "    enabled: true\n"
        "  kimi:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    writer = StateWriter(tmp_path / "writer.db", agents_path=agents_path)
    event = {
        "event_id": "E-kimi-heartbeat",
        "event_type": "agent.kimi.heartbeat",
        "source": "kimi",
        "payload": {
            "lease_ttl_seconds": 90,
            "endpoint": "http://127.0.0.1:8202",
            "skills": ["research"],
        },
    }

    assert writer.apply(event) == "ignored"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'E-kimi-heartbeat';"
    ).fetchone()[0] == 1
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM agents WHERE id = 'kimi';"
    ).fetchone()[0] == 0

    enabled = {**event, "event_id": "E-codex-heartbeat",
               "event_type": "agent.codex.heartbeat", "source": "codex"}
    assert writer.apply(enabled) == "applied"
    assert writer.conn.execute(
        "SELECT status FROM agents WHERE id = 'codex';"
    ).fetchone()[0] == "online"


def test_operator_override_controls_future_heartbeat_registration(tmp_path):
    agents_path = tmp_path / "agents.yaml"
    agents_path.write_text(
        "agents:\n  kimi:\n    enabled: false\n", encoding="utf-8")
    writer = StateWriter(tmp_path / "writer.db", agents_path=agents_path)
    from orchestrator import agent_control_store

    agent_control_store.set_enabled(
        writer.conn, agent_id="kimi", enabled=True, updated_by="test")
    event = {
        "event_id": "E-kimi-enabled-heartbeat",
        "event_type": "agent.kimi.heartbeat", "source": "kimi",
        "payload": {"lease_ttl_seconds": 90,
                    "endpoint": "http://127.0.0.1:8202"},
    }
    assert writer.apply(event) == "applied"
    assert writer.conn.execute(
        "SELECT status FROM agents WHERE id = 'kimi';"
    ).fetchone()[0] == "online"

    agent_control_store.set_enabled(
        writer.conn, agent_id="kimi", enabled=False, updated_by="test")
    second = {**event, "event_id": "E-kimi-disabled-heartbeat"}
    assert writer.apply(second) == "ignored"
    row = writer.conn.execute(
        "SELECT status, lease_expires_at FROM agents WHERE id = 'kimi';"
    ).fetchone()
    assert row["status"] == "disabled"
    assert row["lease_expires_at"] is None
