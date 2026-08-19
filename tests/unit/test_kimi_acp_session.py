"""Kimi ACP permission bridge and event translation tests."""

from __future__ import annotations

import asyncio

import pytest

from adapters.common import A2aTask
from adapters.kimi.session import KimiSessionAdapter
from adapters.session import SessionHandle

pytestmark = pytest.mark.anyio


def _seed_adapter() -> KimiSessionAdapter:
    adapter = KimiSessionAdapter(timeout_seconds=1)
    adapter._handles["S-kimi"] = SessionHandle(
        session_id="S-kimi",
        task_id="T-kimi",
        native_session_id="native-kimi",
        context_revision=3,
    )
    adapter._tasks["S-kimi"] = A2aTask(
        id="T-kimi",
        status_state="working",
        objective="change one file",
        session_id="S-kimi",
        context_revision=3,
    )
    adapter._interactions["S-kimi"] = {}
    adapter._interaction_events["S-kimi"] = asyncio.Event()
    adapter._event_queues["S-kimi"] = asyncio.Queue()
    return adapter


def _permission_request(rpc_id: int = 91) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "native-kimi",
            "options": [
                {"optionId": "yes", "name": "Allow once",
                 "kind": "allow_once"},
                {"optionId": "no", "name": "Reject once",
                 "kind": "reject_once"},
            ],
            "toolCall": {
                "toolCallId": "call-7",
                "title": "Edit app.py",
                "kind": "edit",
                "status": "pending",
                "locations": [{"path": "/workspace/app.py", "line": 1}],
                "rawInput": {"path": "/workspace/app.py"},
            },
        },
    }


async def test_acp_permission_becomes_inspectable_interaction(monkeypatch):
    adapter = _seed_adapter()
    adapter._handle_permission_request(_permission_request())

    pending = adapter.list_pending_interactions("S-kimi")
    assert len(pending) == 1
    interaction = pending[0]
    assert interaction.native_request_id == "91"
    assert interaction.payload["inspectable"] is True
    assert interaction.payload["toolView"]["paths"] == ["/workspace/app.py"]
    assert adapter._interaction_events["S-kimi"].is_set()


async def test_acp_rejection_selects_native_reject_once(monkeypatch):
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    adapter._handle_permission_request(_permission_request())
    interaction = adapter.list_pending_interactions("S-kimi")[0]
    result = await adapter.respond_interaction(
        "S-kimi", interaction.interaction_id,
        {"outcome": "rejected"}, responded_by="hermes")

    assert result.state == "working"
    assert sent == [{
        "jsonrpc": "2.0",
        "id": 91,
        "result": {"outcome": {
            "outcome": "selected", "optionId": "no"}},
    }]
    assert not adapter.list_pending_interactions("S-kimi")


async def test_acp_allow_once_requires_bound_signed_receipt(
        monkeypatch):
    monkeypatch.setenv("LAS_ACTION_RECEIPT_SECRET", "s" * 32)
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    adapter._handle_permission_request(_permission_request())
    interaction = adapter.list_pending_interactions("S-kimi")[0]

    with pytest.raises(PermissionError, match="requires an approved"):
        await adapter.respond_interaction(
            "S-kimi", interaction.interaction_id,
            {"outcome": "allowed-once"}, responded_by="user")

    from common.action_receipt import sign_action_receipt

    receipt = sign_action_receipt({
        "actionIntentId": "AI-1",
        "taskId": "T-kimi",
        "interactionId": interaction.interaction_id,
        "nativeRequestId": "91",
        "nativeSessionId": "native-kimi",
        "contextRevision": 3,
        "status": "approved",
        "decidedBy": "user",
    })
    result = await adapter.respond_interaction(
        "S-kimi", interaction.interaction_id,
        {"outcome": "allowed-once", "authorization": receipt},
        responded_by="user")

    assert result.state == "working"
    assert sent[-1]["result"]["outcome"]["optionId"] == "yes"


async def test_acp_agent_message_chunks_form_summary():
    adapter = _seed_adapter()
    adapter._handle_update({
        "sessionId": "native-kimi",
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "done"},
        },
    })
    assert adapter._assistant_text["S-kimi"] == ["done"]

    adapter._handle_update({
        "sessionId": "native-kimi",
        "update": {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "hidden reasoning"},
        },
    })
    assert all(
        item.get("sessionUpdate") != "agent_thought_chunk"
        for item in adapter._updates["S-kimi"])
    streamed = await anext(adapter.stream_events("S-kimi"))
    assert streamed.event_type == "message.delta"
    assert streamed.payload["content"]["text"] == "done"


async def test_acp_tool_update_drops_raw_payloads():
    adapter = _seed_adapter()
    adapter._handle_update({
        "sessionId": "native-kimi",
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "title": "Edit app.py",
            "status": "completed",
            "locations": [{"path": "/workspace/app.py"}],
            "rawInput": {"api_key": "secret"},
            "rawOutput": "full private output",
            "content": [{"type": "content", "content": {
                "type": "text", "text": "private output"}}],
        },
    })
    saved = adapter._updates["S-kimi"][0]
    assert saved["toolCallId"] == "call-1"
    assert "rawInput" not in saved
    assert "rawOutput" not in saved
    assert "content" not in saved


async def test_acp_process_restart_reloads_native_session(
        monkeypatch, tmp_path):
    monkeypatch.delenv("LAS_KIMI_CLI_MODEL", raising=False)
    adapter = _seed_adapter()
    calls = []

    async def connected():
        return None

    async def rpc(method, params):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_workspace", lambda task_id: tmp_path)
    await adapter._ensure_native_loaded("S-kimi")
    await adapter._ensure_native_loaded("S-kimi")

    assert [method for method, _ in calls] == ["session/load"]
    assert calls[0][1]["sessionId"] == "native-kimi"


async def test_acp_model_override_uses_native_session_method(monkeypatch):
    monkeypatch.setenv("LAS_KIMI_CLI_MODEL", "kimi-for-coding")
    adapter = _seed_adapter()
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(adapter, "_rpc", rpc)
    await adapter._configure_native_session("native-kimi")
    assert calls == [("session/set_model", {
        "sessionId": "native-kimi", "modelId": "kimi-for-coding"})]


async def test_acp_permission_payload_redacts_secret_fields():
    adapter = _seed_adapter()
    request = _permission_request()
    request["params"]["toolCall"]["rawInput"] = {
        "path": "/workspace/app.py",
        "api_key": "must-not-be-audited",
    }
    adapter._handle_permission_request(request)
    interaction = adapter.list_pending_interactions("S-kimi")[0]
    assert interaction.payload["toolView"]["rawInput"]["api_key"] == \
        "[REDACTED]"
