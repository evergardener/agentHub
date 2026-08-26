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


def test_state_writer_uses_explicit_workspace_for_dsh_semantics(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id, objective="review")
    state_store.create_task(
        writer.conn, task_id="T-explicit-dsh", objective="review",
        created_by="hermes", assigned_to="dsh",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(execution_workspace)},
    )
    state_store.transition_task(
        writer.conn, "T-explicit-dsh", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-explicit-dsh", TaskStatus.WORKING)
    writer.apply({
        "event_id": "E-explicit-dsh", "event_type": "task.input_required",
        "source": "dsh", "task_id": "T-explicit-dsh",
        "payload": {
            "session_id": "S-explicit-dsh",
            "native_session_id": "native-explicit-dsh",
            "interactions": [{
                "interactionId": "dsh:explicit", "kind": "approval",
                "nativeRequestId": "rpc-explicit",
                "nativeSessionId": "native-explicit-dsh",
                "payload": {
                    "toolName": "bash", "inspectable": True,
                    "toolView": {
                        "card": "terminal", "command": "touch safe.txt",
                        "cwd": str(execution_workspace),
                    },
                },
            }],
        },
    })

    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-explicit-dsh")[0]
    persisted = json.loads(interaction["payload_json"])
    assert persisted["toolView"]["semanticIntent"]["targets"][
        "workspace"] == str(execution_workspace.resolve())


def test_state_writer_verifies_codex_file_change_for_hermes_route(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    target = execution_workspace / "src" / "app.py"
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id, objective="implement")
    state_store.create_task(
        writer.conn, task_id="T-codex", objective="implement",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(execution_workspace)},
    )
    state_store.transition_task(writer.conn, "T-codex", TaskStatus.ASSIGNED)
    state_store.transition_task(writer.conn, "T-codex", TaskStatus.WORKING)
    writer.apply({
        "event_id": "E-codex-edit", "event_type": "task.input_required",
        "source": "codex", "task_id": "T-codex",
        "payload": {
            "session_id": "S-codex", "native_session_id": "native-codex",
            "interactions": [{
                "interactionId": "codex:edit", "kind": "approval",
                "nativeRequestId": "rpc-edit",
                "nativeSessionId": "native-codex",
                "payload": {
                    "toolName": "edit", "inspectable": True,
                    "reason": "apply patch",
                    "toolView": {
                        "kind": "edit", "paths": [str(target)],
                        "changes": [{
                            "path": str(target),
                            "kind": {"type": "update"},
                            "diff": "@@ -1 +1 @@\n-old\n+new",
                        }],
                    },
                },
            }],
        },
    })

    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-codex")[0]
    intent = writer.conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;",
        (interaction["action_intent_id"],),
    ).fetchone()
    assert intent["operation"] == "filesystem.write"
    assert intent["status"] == "awaiting_hermes"
    assert intent["policy_route"] == "hermes"


def test_state_writer_structures_codex_login_shell_for_user_approval(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="build the image")
    state_store.create_task(
        writer.conn, task_id="T-codex-command", objective="build",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(execution_workspace)},
    )
    state_store.transition_task(
        writer.conn, "T-codex-command", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-codex-command", TaskStatus.WORKING)

    writer.apply({
        "event_id": "E-codex-command",
        "event_type": "task.input_required",
        "source": "codex",
        "task_id": "T-codex-command",
        "payload": {
            "session_id": "S-codex-command",
            "native_session_id": "native-codex-command",
            "interactions": [{
                "interactionId": "codex:command",
                "kind": "approval",
                "nativeRequestId": "rpc-command",
                "nativeSessionId": "native-codex-command",
                "payload": {
                    "toolName": "shell",
                    "inspectable": True,
                    "reason": "build image",
                    "toolView": {
                        "kind": "shell",
                        "command": "/bin/zsh -lc 'docker build .'",
                        "cwd": str(execution_workspace),
                    },
                },
            }],
        },
    })

    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-codex-command")[0]
    payload = json.loads(interaction["payload_json"])
    intent = writer.conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;",
        (interaction["action_intent_id"],),
    ).fetchone()
    targets = json.loads(intent["targets_json"])

    assert payload["inspectable"] is True
    assert intent["operation"] == "command.execute"
    assert intent["status"] == "awaiting_user"
    assert intent["policy_route"] == "user"
    assert targets["workspace"] == str(execution_workspace.resolve())
    assert targets["cwd"] == str(execution_workspace.resolve())
    assert targets["command"] == "docker"
    assert targets["args"] == ["build", "."]


def test_state_writer_routes_structured_codex_docker_ps_to_hermes_once(
        tmp_path, monkeypatch):
    """A safe Docker read is Hermes-owned, not silently user-approved.

    StateWriter persists the signed-approval boundary; the supervising Hermes
    session is responsible for the native one-shot response.  This catches the
    regression where a new read operation is either classified as critical or
    marked approved without any native delivery path.
    """
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    writer = StateWriter(tmp_path / "state.db")
    conversation_id = collaboration_store.create_conversation(
        writer.conn, title="Grafana read-only investigation")
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="inspect Docker state")
    state_store.create_task(
        writer.conn, task_id="T-codex-docker-read", objective="inspect",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(execution_workspace)},
    )
    state_store.transition_task(
        writer.conn, "T-codex-docker-read", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-codex-docker-read", TaskStatus.WORKING)

    writer.apply({
        "event_id": "E-codex-docker-read",
        "event_type": "task.input_required",
        "source": "codex",
        "task_id": "T-codex-docker-read",
        "payload": {
            "session_id": "S-codex-docker-read",
            "native_session_id": "native-codex-docker-read",
            "interactions": [{
                "interactionId": "codex:docker-ps",
                "kind": "approval",
                "nativeRequestId": "rpc-docker-ps",
                "nativeSessionId": "native-codex-docker-read",
                "payload": {
                    "toolName": "shell",
                    "inspectable": True,
                    "reason": "discover running containers",
                    "toolView": {
                        "kind": "shell",
                        "command": "/bin/zsh -lc 'docker ps'",
                        "cwd": str(execution_workspace),
                    },
                },
            }],
        },
    })

    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-codex-docker-read")[0]
    payload = json.loads(interaction["payload_json"])
    intent = writer.conn.execute(
        "SELECT * FROM action_intents WHERE id = ?;",
        (interaction["action_intent_id"],),
    ).fetchone()
    assert payload["inspectable"] is True
    assert intent["operation"] == "command.read"
    assert intent["risk"] == "read"
    assert intent["policy_route"] == "hermes"
    assert intent["status"] == "awaiting_hermes"
    assert intent["decided_by"] is None

    pending = collaboration_store.pending_interaction_views(
        writer.conn, "T-codex-docker-read")
    assert pending[0]["interaction_id"] == interaction["id"]
    assert pending[0]["action_intent_status"] == "awaiting_hermes"
    assert pending[0]["operation"] == "command.read"
    assert pending[0]["risk"] == "read"
