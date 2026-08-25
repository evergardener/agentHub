"""Codex wrapped native command -> State Writer -> one-shot approval."""

from __future__ import annotations

import json

import pytest

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store
from orchestrator.task_manager import TaskManager
from state.writer import StateWriter

pytestmark = pytest.mark.anyio


async def test_wrapped_codex_command_reaches_user_one_shot_approval(
        tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "codex-wrapper-test-secret-0123456789")

    writer = StateWriter(db_path)
    conversation_id = collaboration_store.create_conversation(writer.conn)
    collaboration_id = collaboration_store.create_collaboration(
        writer.conn, conversation_id=conversation_id,
        objective="build image")
    state_store.create_task(
        writer.conn, task_id="T-codex-wrapper", objective="build image",
        created_by="hermes", assigned_to="codex",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(workspace)},
    )
    state_store.transition_task(
        writer.conn, "T-codex-wrapper", TaskStatus.ASSIGNED)
    state_store.transition_task(
        writer.conn, "T-codex-wrapper", TaskStatus.WORKING)
    state_store.update_heartbeat(
        writer.conn, "codex", endpoint="http://codex-adapter:8201",
        lease_ttl_seconds=90)

    writer.apply({
        "event_id": "E-codex-wrapper",
        "event_type": "task.input_required",
        "source": "codex",
        "task_id": "T-codex-wrapper",
        "payload": {
            "session_id": "S-codex-wrapper",
            "native_session_id": "native-codex-wrapper",
            "capabilities": {"native_resume": True},
            "interactions": [{
                "interactionId": "I-codex-wrapper",
                "kind": "approval",
                "nativeRequestId": "rpc-codex-wrapper",
                "nativeSessionId": "native-codex-wrapper",
                "payload": {
                    "toolName": "shell",
                    "reason": "run production-equivalent image build",
                    "toolView": {
                        "kind": "shell",
                        "command": "/bin/zsh -lc 'docker build .'",
                        "cwd": str(workspace),
                    },
                },
            }],
        },
    })
    interaction = collaboration_store.list_session_interactions(
        writer.conn, task_id="T-codex-wrapper")[0]
    payload = json.loads(interaction["payload_json"])
    assert payload["inspectable"] is True
    writer.conn.close()

    captured = {}

    class FakeClient:
        async def respond_interaction(
                self, task_id, adapter_interaction_id, response, *,
                responded_by):
            captured.update({
                "task_id": task_id,
                "interaction_id": adapter_interaction_id,
                "response": response,
                "responded_by": responded_by,
            })
            return {"id": task_id, "status": {"state": "working"}}

    monkeypatch.setattr(
        "orchestrator.task_manager.A2aClient.for_agent",
        lambda *args, **kwargs: FakeClient())
    manager = TaskManager(db_path=db_path)
    try:
        result = await manager.respond_agent_interaction(
            interaction["id"], response={"outcome": "allowed-once"},
            requested_by="user")
        assert result["status"]["state"] == "working"
        assert captured["interaction_id"] == "I-codex-wrapper"
        assert captured["responded_by"] == "user"
        receipt = captured["response"]["authorization"]
        assert receipt["status"] == "approved"
        assert receipt["nativeRequestId"] == "rpc-codex-wrapper"
        assert receipt["nativeSessionId"] == "native-codex-wrapper"
    finally:
        manager.close()
