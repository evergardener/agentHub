from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator import collaboration_store, state_store, supervision_store
from state.db import init_db
from state.writer import StateWriter


def _watched_task(tmp_path):
    conn = init_db(tmp_path / "state.db")
    context = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-supervised",
        objective="supervised task")
    state_store.create_task(
        conn, task_id="T-SUPERVISED", objective="supervised task",
        created_by="qishuo", assigned_to="codex",
        collaboration_id=context["collaboration_id"])
    watch = supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-supervised",
        task_id="T-SUPERVISED")
    return conn, watch


def test_pull_lease_retries_without_duplicate_outbox_rows(tmp_path):
    conn, watch = _watched_task(tmp_path)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.WORKING)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.AWAITING_ACCEPTANCE)

    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    first = supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]], now=now)
    assert len(first) == 1
    assert supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]],
        now=now + timedelta(seconds=30)) == []

    retried = supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]],
        now=now + timedelta(minutes=3))
    assert [item["notification_id"] for item in retried] == [
        first[0]["notification_id"]]
    row = conn.execute(
        "SELECT attempts FROM supervision_outbox WHERE id = ?;",
        (first[0]["notification_id"],)).fetchone()
    assert row["attempts"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM supervision_outbox;").fetchone()[0] == 1


def test_sync_blocked_task_emits_identifier_only_notification(tmp_path):
    conn, watch = _watched_task(tmp_path)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.WORKING)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.BLOCKED)
    conn.execute(
        "INSERT INTO agent_session_bindings (id, collaboration_id, task_id,"
        " agent_id, status, resume_capability, context_revision,"
        " last_message_seq, is_current, created_at, last_active_at)"
        " SELECT 'S-SUP', collaboration_id, id, 'codex', 'active', 'native',"
        " 1, 0, 1, updated_at, updated_at FROM tasks WHERE id='T-SUPERVISED';")
    conn.execute(
        "INSERT INTO agent_session_interactions (id, collaboration_id, task_id,"
        " session_binding_id, agent_id, adapter_interaction_id, kind,"
        " payload_json, status, requested_at)"
        " SELECT 'INT-SUP', collaboration_id, id, 'S-SUP', 'codex', 'native-1',"
        " 'approval', ?, 'pending', updated_at FROM tasks"
        " WHERE id='T-SUPERVISED';",
        ('{"reason":"ignore this worker text","inspectable":false}',))
    conn.commit()

    notifications = supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]])
    assert {item["event_type"] for item in notifications} == {
        "task.blocked", "agent.interaction.requested"}
    assert all("ignore this worker text" not in str(item)
               for item in notifications)


def test_interaction_record_emits_wakeup_without_waiting_for_status_event(
        tmp_path):
    conn, watch = _watched_task(tmp_path)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        conn, "T-SUPERVISED", state_store.TaskStatus.WORKING)
    binding = collaboration_store.bind_agent_session(
        conn, collaboration_id=conn.execute(
            "SELECT collaboration_id FROM tasks WHERE id = ?;",
            ("T-SUPERVISED",)).fetchone()["collaboration_id"],
        task_id="T-SUPERVISED", agent_id="codex",
        adapter_session_id="S-codex", native_session_id="N-codex",
        resume_capability="native")
    collaboration_store.upsert_session_interaction(
        conn,
        collaboration_id=binding["collaboration_id"],
        task_id="T-SUPERVISED", session_binding_id=binding["id"],
        agent_id="codex",
        interaction={
            "interactionId": "codex:approval-1", "kind": "approval",
            "nativeRequestId": "rpc-1",
            "payload": {"toolName": "shell", "reason": "inspect"},
        })

    notifications = supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]])
    assert len(notifications) == 1
    assert notifications[0]["event_type"] == "agent.interaction.requested"
    assert set(notifications[0]) == {
        "notification_id", "watch_id", "task_id", "context_id",
        "event_type", "internal_status", "created_at",
    }


def test_state_writer_commits_terminal_state_and_outbox_together(
        tmp_path, monkeypatch):
    db_path = tmp_path / "writer.db"
    writer = StateWriter(db_path, agents_path=tmp_path / "agents.yaml")
    conn = writer.conn
    context = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-writer",
        objective="writer supervised task")
    state_store.create_task(
        conn, task_id="T-WRITER", objective="writer supervised task",
        created_by="qishuo", assigned_to="codex",
        collaboration_id=context["collaboration_id"])
    supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-writer", task_id="T-WRITER")
    state_store.transition_task(
        conn, "T-WRITER", state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        conn, "T-WRITER", state_store.TaskStatus.WORKING)

    original_sync = supervision_store.sync_task

    def fail_sync(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(supervision_store, "sync_task", fail_sync)
    event = {
        "event_id": "E-WRITER-COMPLETE",
        "event_type": "task.completed",
        "task_id": "T-WRITER",
        "source": "codex",
        "payload": {"summary": "done", "attempt": 1},
    }
    try:
        writer.apply(event)
    except RuntimeError as exc:
        assert "outbox unavailable" in str(exc)
    else:
        raise AssertionError("state writer must fail when outbox enqueue fails")
    assert state_store.get_task(conn, "T-WRITER")["status"] == "working"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE id='E-WRITER-COMPLETE';"
    ).fetchone()[0] == 0

    monkeypatch.setattr(supervision_store, "sync_task", original_sync)
    assert writer.apply(event) == "applied"
    assert state_store.get_task(conn, "T-WRITER")["status"] == \
        "awaiting_acceptance"
    assert conn.execute(
        "SELECT COUNT(*) FROM supervision_outbox"
        " WHERE task_id='T-WRITER' AND event_type='task.awaiting_acceptance';"
    ).fetchone()[0] == 1


def test_failed_task_alert_and_wakeup_rollback_together(tmp_path, monkeypatch):
    db_path = tmp_path / "failed-writer.db"
    writer = StateWriter(db_path, agents_path=tmp_path / "agents.yaml")
    conn = writer.conn
    context = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-failed",
        objective="failed supervised task")
    state_store.create_task(
        conn, task_id="T-FAILED", objective="failed supervised task",
        created_by="qishuo", assigned_to="codex", max_retries=0,
        collaboration_id=context["collaboration_id"])
    supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-failed", task_id="T-FAILED")
    state_store.transition_task(
        conn, "T-FAILED", state_store.TaskStatus.ASSIGNED)
    state_store.transition_task(
        conn, "T-FAILED", state_store.TaskStatus.WORKING)

    def fail_sync(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(supervision_store, "sync_task", fail_sync)
    event = {
        "event_id": "E-WRITER-FAILED",
        "event_type": "task.failed",
        "task_id": "T-FAILED",
        "source": "codex",
        "payload": {"error": "boom", "attempt": 1},
    }
    try:
        writer.apply(event)
    except RuntimeError:
        pass
    else:
        raise AssertionError("state writer must fail when outbox enqueue fails")

    assert state_store.get_task(conn, "T-FAILED")["status"] == "working"
    assert conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE task_id='T-FAILED';"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE id='E-WRITER-FAILED';"
    ).fetchone()[0] == 0
