"""Fault injection proves event dedupe and state mutation commit atomically."""

from __future__ import annotations

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from state.writer import StateWriter


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
