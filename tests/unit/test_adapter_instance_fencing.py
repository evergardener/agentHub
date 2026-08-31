"""A replacement worker instance fences tasks owned by the lost instance."""

from __future__ import annotations

import pytest

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store, supervision_store
from orchestrator.a2a_server import _to_a2a
from state.writer import StateWriter


def _heartbeat(event_id: str, instance_id: str, started_at: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": "agent.codex.heartbeat",
        "source": "codex",
        "payload": {
            "lease_ttl_seconds": 90,
            "adapterInstanceId": instance_id,
            "adapterStartedAt": started_at,
        },
    }


@pytest.mark.parametrize(
    "started_at", ["garbage", "2026-08-29T12:00:00"],
    ids=["garbage-timestamp", "naive-timestamp"],
)
def test_invalid_first_generation_does_not_poison_canonical_heartbeat(
        tmp_path, started_at):
    writer = StateWriter(tmp_path / f"invalid-{started_at}.db")

    assert writer.apply(_heartbeat(
        "HB-invalid", "codex-invalid", started_at)) == "ignored"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM agents WHERE id = 'codex';"
    ).fetchone()[0] == 0
    assert writer.audit_log[-1]["reason"] == \
        "invalid adapter instance generation heartbeat ignored"

    valid_started_at = "2026-08-29T13:00:00+00:00"
    assert writer.apply(_heartbeat(
        "HB-valid", "codex-valid", valid_started_at)) == "applied"
    agent = writer.conn.execute(
        "SELECT adapter_instance_id, adapter_started_at FROM agents"
        " WHERE id = 'codex';"
    ).fetchone()
    assert agent["adapter_instance_id"] == "codex-valid"
    assert agent["adapter_started_at"] == valid_started_at


def test_new_adapter_instance_fails_old_active_task_and_notifies_supervisor(
        tmp_path):
    writer = StateWriter(tmp_path / "state.db")
    context = collaboration_store.ensure_a2a_collaboration(
        writer.conn, peer="qishuo", context_id="ctx-instance-fence",
        objective="long-running task")
    task_id = "T-INSTANCE-FENCE"
    state_store.create_task(
        writer.conn, task_id=task_id, objective="long-running task",
        created_by="qishuo", assigned_to="codex",
        collaboration_id=context["collaboration_id"])
    state_store.transition_task(writer.conn, task_id, TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, task_id, TaskStatus.WORKING)
    collaboration_store.bind_agent_session(
        writer.conn, collaboration_id=context["collaboration_id"],
        task_id=task_id, agent_id="codex", adapter_session_id="S-old",
        adapter_instance_id="codex-old")
    watch = supervision_store.register_watch(
        writer.conn, peer="qishuo", context_id="ctx-instance-fence",
        task_id=task_id)

    old_started = "2026-08-29T10:00:00+00:00"
    new_started = "2026-08-29T11:00:00+00:00"
    assert writer.apply(_heartbeat(
        "HB-old", "codex-old", old_started)) == "applied"
    assert state_store.get_task(writer.conn, task_id)["status"] == "working"
    assert writer.apply(_heartbeat(
        "HB-new", "codex-new", new_started)) == "applied"

    task = state_store.get_task(writer.conn, task_id)
    assert task["status"] == "failed"
    assert "codex-old -> codex-new" in task["error_message"]
    binding = collaboration_store.get_current_agent_session(
        writer.conn, task_id, "codex")
    assert binding["status"] == "interrupted"
    assert binding["recovery_state"] == "adapter_instance_replaced"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE task_id = ?"
        " AND kind = 'adapter_instance_replaced';", (task_id,)
    ).fetchone()[0] == 1
    failure_event = writer.conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.failed';", (task_id,)
    ).fetchone()
    assert "adapter_instance_replaced" in failure_event["payload_json"]
    notifications = supervision_store.pull_notifications(
        writer.conn, peer="qishuo", watch_ids=[watch["watch_id"]])
    assert "task.failed" in {item["event_type"] for item in notifications}
    message = writer.conn.execute(
        "SELECT message_type, content_json FROM conversation_messages"
        " WHERE task_id = ? AND sender_type = 'agent'"
        " ORDER BY sequence DESC LIMIT 1;", (task_id,)
    ).fetchone()
    assert message["message_type"] == "agent.task.error"
    assert "adapter instance was replaced" in message["content_json"]

    replacement_task = "T-INSTANCE-NEW"
    state_store.create_task(
        writer.conn, task_id=replacement_task, objective="replacement work",
        created_by="qishuo", assigned_to="codex",
        collaboration_id=context["collaboration_id"])
    state_store.transition_task(
        writer.conn, replacement_task, TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, replacement_task, TaskStatus.WORKING)
    collaboration_store.bind_agent_session(
        writer.conn, collaboration_id=context["collaboration_id"],
        task_id=replacement_task, agent_id="codex",
        adapter_session_id="S-new", adapter_instance_id="codex-new")

    assert writer.apply(_heartbeat(
        "HB-old-late", "codex-old", old_started)) == "ignored"
    assert state_store.get_task(
        writer.conn, replacement_task)["status"] == "working"
    agent = writer.conn.execute(
        "SELECT adapter_instance_id, adapter_started_at FROM agents"
        " WHERE id = 'codex';").fetchone()
    assert agent["adapter_instance_id"] == "codex-new"
    assert agent["adapter_started_at"] == new_started


def test_runtime_failure_closes_persistent_interaction_and_wakes_supervisor(
        tmp_path):
    writer = StateWriter(tmp_path / "runtime-failure.db")
    context = collaboration_store.ensure_a2a_collaboration(
        writer.conn, peer="qishuo", context_id="ctx-runtime-failure",
        objective="blocked native turn")
    task_id = "T-RUNTIME-FAILURE"
    state_store.create_task(
        writer.conn, task_id=task_id, objective="blocked native turn",
        created_by="qishuo", assigned_to="codex",
        collaboration_id=context["collaboration_id"])
    state_store.transition_task(writer.conn, task_id, TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, task_id, TaskStatus.WORKING)
    state_store.transition_task(writer.conn, task_id, TaskStatus.BLOCKED)
    binding = collaboration_store.bind_agent_session(
        writer.conn, collaboration_id=context["collaboration_id"],
        task_id=task_id, agent_id="codex", adapter_session_id="S-runtime",
        adapter_instance_id="codex-runtime")
    interaction = collaboration_store.upsert_session_interaction(
        writer.conn, collaboration_id=context["collaboration_id"],
        task_id=task_id, session_binding_id=binding["id"], agent_id="codex",
        interaction={
            "interactionId": "native-approval-1", "kind": "approval",
            "nativeRequestId": "rpc-1",
            "payload": {"inspectable": True, "toolName": "filesystem.write"},
        })
    watch = supervision_store.register_watch(
        writer.conn, peer="qishuo", context_id="ctx-runtime-failure",
        task_id=task_id)

    result = writer.apply({
        "event_id": "EV-runtime-failure",
        "event_type": "task.failed",
        "task_id": task_id,
        "source": "codex",
        "payload": {
            "status_from": "blocked", "status_to": "failed",
            "attempt": 1, "error": "Codex native runtime unavailable",
            "reason": "native_runtime_unavailable",
            "interaction_ids": ["native-approval-1"],
        },
    })

    assert result == "applied"
    assert state_store.get_task(writer.conn, task_id)["status"] == "failed"
    closed = collaboration_store.get_session_interaction(
        writer.conn, interaction["id"])
    assert closed["status"] == "cancelled"
    assert closed["resolved_by"] == "system"
    assert "native runtime unavailable" in closed["last_error"]
    assert collaboration_store.pending_interaction_views(
        writer.conn, task_id) == []
    wire = _to_a2a(writer.conn, state_store.get_task(writer.conn, task_id))
    assert wire["status"]["state"] == "failed"
    notifications = supervision_store.pull_notifications(
        writer.conn, peer="qishuo", watch_ids=[watch["watch_id"]])
    assert "task.failed" in {item["event_type"] for item in notifications}


def test_stale_execution_generation_cannot_fail_replacement_dispatch(tmp_path):
    writer = StateWriter(tmp_path / "execution-generation.db")
    task_id = "T-EXECUTION-GENERATION"
    state_store.create_task(
        writer.conn, task_id=task_id, objective="replacement dispatch",
        created_by="qishuo", assigned_to="codex")
    state_store.transition_task(writer.conn, task_id, TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, task_id, TaskStatus.WORKING)
    writer.conn.execute(
        "UPDATE tasks SET execution_generation = ? WHERE id = ?;",
        ("EX-new", task_id),
    )
    writer.conn.commit()

    missing = writer.apply({
        "event_id": "EV-missing-generation", "event_type": "task.failed",
        "task_id": task_id, "source": "codex",
        "payload": {
            "status_from": "working", "status_to": "failed",
            "error": "legacy delayed failure",
        },
    })
    assert missing == "ignored"
    assert state_store.get_task(writer.conn, task_id)["status"] == "working"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'EV-missing-generation';"
    ).fetchone()[0] == 0

    stale_interaction = writer.apply({
        "event_id": "EV-old-interaction",
        "event_type": "agent.interaction.requested",
        "task_id": task_id, "source": "codex",
        "payload": {
            "execution_generation": "EX-old",
            "interactions": [{
                "interactionId": "old-approval", "kind": "approval",
                "payload": {"inspectable": True},
            }],
        },
    })
    assert stale_interaction == "ignored"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'EV-old-interaction';"
    ).fetchone()[0] == 0

    stale = writer.apply({
        "event_id": "EV-old-failure", "event_type": "task.failed",
        "task_id": task_id, "source": "codex",
        "payload": {
            "status_from": "working", "status_to": "failed",
            "error": "old dispatch failed",
            "execution_generation": "EX-old",
        },
    })
    assert stale == "ignored"
    assert state_store.get_task(writer.conn, task_id)["status"] == "working"
    assert writer.conn.execute(
        "SELECT COUNT(*) FROM events WHERE id = 'EV-old-failure';"
    ).fetchone()[0] == 0

    current = writer.apply({
        "event_id": "EV-new-failure", "event_type": "task.failed",
        "task_id": task_id, "source": "codex",
        "payload": {
            "status_from": "working", "status_to": "failed",
            "error": "current dispatch failed",
            "execution_generation": "EX-new",
        },
    })
    assert current == "applied"
    assert state_store.get_task(writer.conn, task_id)["status"] == "failed"
