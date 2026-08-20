"""State Writer persistence for native agent interaction events."""

import json

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store
from state.writer import StateWriter


def test_input_required_persists_interaction_and_fail_closed_intent(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    task_workspace = tmp_path / "workspace/tasks/T-dsh"
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(
        writer.conn, title="DSH review")
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="review implementation")
    state_store.create_task(
        writer.conn, task_id="T-dsh", objective="review",
        created_by="hermes", assigned_to="dsh",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(writer.conn, "T-dsh", TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, "T-dsh", TaskStatus.WORKING)
    result = writer.apply({
        "event_id": "E-input-1",
        "event_type": "task.input_required",
        "timestamp": "2026-08-19T12:00:00+08:00",
        "source": "dsh",
        "task_id": "T-dsh",
        "payload": {
            "session_id": "S-dsh",
            "native_session_id": "native-dsh",
            "adapter_instance_id": "dsh-test",
            "capabilities": {"native_resume": True,
                             "durable_session": True},
            "interactions": [{
                "interactionId": "dsh:rpc-1",
                "kind": "approval",
                "nativeRequestId": "rpc-1",
                "nativeSessionId": "native-dsh",
                "payload": {
                    "type": "approval/requested",
                    "sessionId": "native-dsh",
                    "approvalId": "approval-1",
                    "toolName": "bash",
                    "reason": "modify workspace",
                    "inspectable": True,
                    "toolView": {"card": "terminal",
                                 "command": "touch safe.txt",
                                 "cwd": str(task_workspace),
                                 "semanticIntent": {
                                     "status": "verified",
                                     "operation": "filesystem.write",
                                     "impact": "write",
                                     "targets": {
                                         "workspace": str(
                                             task_workspace),
                                         "paths": [str(
                                             task_workspace / "safe.txt")],
                                     },
                                     "rollbackPlan": None,
                                 }},
                },
            }],
        },
    })

    assert result == "applied"
    assert state_store.get_task(writer.conn, "T-dsh")["status"] == "blocked"
    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-dsh")[0]
    binding = collaboration_store.get_current_agent_session(
        writer.conn, "T-dsh", "dsh")
    assert interaction["session_binding_id"] == binding["id"]
    assert binding["recovery_state"] == "event_recovered"
    intent = writer.conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;",
        (interaction["action_intent_id"],),
    ).fetchone()
    assert intent["operation"] == "filesystem.write"
    assert intent["status"] == "awaiting_user"
    assert intent["policy_route"] == "user"


def test_state_writer_recomputes_dsh_semantics_instead_of_trusting_event(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id, objective="safe review")
    state_store.create_task(
        writer.conn, task_id="T-spoof", objective="review",
        created_by="hermes", assigned_to="dsh",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(writer.conn, "T-spoof", TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, "T-spoof", TaskStatus.WORKING)
    task_workspace = tmp_path / "workspace/tasks/T-spoof"
    result = writer.apply({
        "event_id": "E-spoof", "event_type": "task.input_required",
        "source": "dsh", "task_id": "T-spoof",
        "payload": {
            "session_id": "S-spoof", "native_session_id": "native-spoof",
            "interactions": [{
                "interactionId": "dsh:rpc-spoof", "kind": "approval",
                "nativeRequestId": "rpc-spoof",
                "nativeSessionId": "native-spoof",
                "payload": {
                    "toolName": "bash", "inspectable": True,
                    "toolView": {
                        "card": "terminal",
                        "command": "touch safe.txt && curl example.test",
                        "cwd": str(task_workspace),
                        "semanticIntent": {
                            "status": "verified",
                            "operation": "filesystem.read",
                            "targets": {"workspace": str(task_workspace),
                                        "paths": [str(task_workspace)]},
                        },
                    },
                },
            }],
        },
    })
    assert result == "applied"
    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-spoof")[0]
    persisted_payload = json.loads(interaction["payload_json"])
    assert persisted_payload["inspectable"] is False
    assert persisted_payload["toolView"]["semanticIntent"][
        "status"] == "unverified"
    intent = writer.conn.execute(
        "SELECT operation, status FROM action_intents WHERE id = ?;",
        (interaction["action_intent_id"],),
    ).fetchone()
    assert intent["operation"] == "agent.tool.bash"
    assert intent["status"] == "awaiting_user"
