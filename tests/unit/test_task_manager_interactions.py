"""Hermes authority checks for native agent interaction responses."""

import pytest

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store
from orchestrator.task_manager import TaskManager
from state.db import init_db

pytestmark = pytest.mark.anyio


async def test_user_approval_delivers_action_intent_receipt(
        tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "test-secret-0123456789abcdef")
    conn = init_db(db)
    conversation_id = collaboration_store.create_conversation(conn)
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id, objective="modify")
    state_store.create_task(
        conn, task_id="T-approval", objective="modify",
        created_by="hermes", assigned_to="dsh",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    state_store.transition_task(conn, "T-approval", TaskStatus.ASSIGNED)
    state_store.transition_task(conn, "T-approval", TaskStatus.WORKING)
    state_store.transition_task(conn, "T-approval", TaskStatus.BLOCKED)
    state_store.update_heartbeat(
        conn, "dsh", endpoint="http://dsh-adapter:8203",
        lease_ttl_seconds=90)
    binding = collaboration_store.bind_agent_session(
        conn, collaboration_id=collaboration_id, task_id="T-approval",
        agent_id="dsh", adapter_session_id="S-dsh",
        native_session_id="native-dsh", resume_capability="native")
    interaction = collaboration_store.upsert_session_interaction(
        conn, collaboration_id=collaboration_id, task_id="T-approval",
        session_binding_id=binding["id"], agent_id="dsh",
        interaction={
            "interactionId": "dsh:rpc-1", "kind": "approval",
            "nativeRequestId": "rpc-1",
            "payload": {"approvalId": "approval-1", "toolName": "bash",
                        "inspectable": True,
                        "toolView": {"card": "terminal",
                                     "command": "touch safe.txt"}},
        })
    intent = collaboration_store.request_action_intent(
        conn, collaboration_id=collaboration_id, task_id="T-approval",
        session_binding_id=binding["id"], requested_by_agent_id="dsh",
        operation="agent.tool.bash", targets={"workspace": str(tmp_path)},
        purpose="modify", expected_effects={"toolName": "bash"},
        based_on_revision=1)
    collaboration_store.attach_action_intent(
        conn, interaction["id"], intent["id"])
    conn.close()

    captured = {}

    class FakeClient:
        async def respond_interaction(
                self, task_id, adapter_interaction_id, response, *,
                responded_by):
            captured.update({
                "task_id": task_id,
                "adapter_interaction_id": adapter_interaction_id,
                "response": response,
                "responded_by": responded_by,
            })
            return {"id": task_id, "status": {"state": "working"}}

    monkeypatch.setattr(
        "orchestrator.task_manager.A2aClient.for_agent",
        lambda *args, **kwargs: FakeClient())
    manager = TaskManager(db_path=db)
    try:
        with pytest.raises(ValueError, match="native agent interaction"):
            manager.approve_task("T-approval")
        result = await manager.respond_agent_interaction(
            interaction["id"], response={"outcome": "allowed-once"},
            requested_by="user")
        assert result["status"]["state"] == "working"
        assert captured["adapter_interaction_id"] == "dsh:rpc-1"
        receipt = captured["response"]["authorization"]
        assert receipt["actionIntentId"] == intent["id"]
        assert receipt["status"] == "approved"
        assert receipt["decidedBy"] == "user"
        assert receipt["nativeSessionId"] == "native-dsh"
        assert receipt["contextRevision"] == 1
        saved = collaboration_store.get_session_interaction(
            manager.conn, interaction["id"])
        assert saved["status"] == "resolved"
    finally:
        manager.close()
