"""A2A client AgentHub session metadata/control encoding."""

import pytest

from orchestrator.a2a_client import A2aClient

pytestmark = pytest.mark.anyio


async def test_send_encodes_session_recovery_metadata(monkeypatch):
    client = A2aClient("http://adapter")
    captured = {}

    async def fake_rpc(payload):
        captured.update(payload)
        return {"id": "T-1"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    await client.send_message(
        "continue", task_id="T-1", session_id="S-1",
        native_session_id="N-1", context_revision=7,
        replace_session=True, metadata={"recoveryMode": "native_resume"})
    metadata = captured["params"]["message"]["metadata"]
    assert metadata == {
        "recoveryMode": "native_resume", "taskId": "T-1",
        "sessionId": "S-1", "nativeSessionId": "N-1",
        "contextRevision": 7, "replaceSession": True,
    }


@pytest.mark.parametrize(("operation", "method"), [
    ("pause", "extensions/session/pause"),
    ("resume", "extensions/session/resume"),
    ("interrupt", "extensions/session/interrupt"),
    ("cancel", "tasks/cancel"),
])
async def test_control_method_mapping(monkeypatch, operation, method):
    client = A2aClient("http://adapter")
    captured = {}

    async def fake_rpc(payload):
        captured.update(payload)
        return {"id": "T-1"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    await client.control_session("T-1", operation)
    assert captured["method"] == method
    assert captured["params"] == {"id": "T-1"}


async def test_interaction_response_encoding(monkeypatch):
    client = A2aClient("http://adapter")
    captured = {}

    async def fake_rpc(payload):
        captured.update(payload)
        return {"id": "T-1"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    await client.respond_interaction(
        "T-1", "dsh:rpc-1", {"outcome": "rejected"},
        responded_by="hermes")
    assert captured["method"] == \
        "extensions/session/interactions/respond"
    assert captured["params"] == {
        "id": "T-1", "interactionId": "dsh:rpc-1",
        "response": {"outcome": "rejected"},
        "respondedBy": "hermes",
    }


async def test_steer_encodes_revision_and_message(monkeypatch):
    client = A2aClient("http://adapter")
    captured = {}

    async def fake_rpc(payload):
        captured.update(payload)
        return {"id": "T-1"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    await client.steer_session(
        "T-1", "不要改数据库", context_revision=8, message_id="M-steer")
    assert captured["method"] == "extensions/session/steer"
    assert captured["params"]["message"] == {
        "role": "user",
        "parts": [{"kind": "text", "text": "不要改数据库"}],
        "metadata": {"messageId": "M-steer", "contextRevision": 8},
    }
