"""Codex App Server approval bridge and event translation tests."""

from __future__ import annotations

import asyncio

import pytest

from adapters.codex.session import CodexSessionAdapter
from adapters.common import A2aTask
from adapters.session import SessionCapabilityError, SessionHandle

pytestmark = pytest.mark.anyio


def _seed_adapter() -> CodexSessionAdapter:
    adapter = CodexSessionAdapter(timeout_seconds=1)
    adapter._handles["S-codex"] = SessionHandle(
        session_id="S-codex", task_id="T-codex",
        native_session_id="native-codex", context_revision=4,
    )
    adapter._tasks["S-codex"] = A2aTask(
        id="T-codex", status_state="working", objective="change one file",
        session_id="S-codex", context_revision=4,
    )
    adapter._interactions["S-codex"] = {}
    adapter._interaction_events["S-codex"] = asyncio.Event()
    return adapter


def _command_request(rpc_id: int = 41) -> dict:
    return {
        "jsonrpc": "2.0", "id": rpc_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "native-codex", "turnId": "turn-1",
            "itemId": "item-7", "startedAtMs": 1,
            "command": "pytest tests/unit/test_app.py",
            "cwd": "/workspace", "reason": "run targeted tests",
            "commandActions": [{
                "type": "read", "path": "/workspace/tests/unit/test_app.py",
                "name": "test_app.py", "command": "pytest",
            }],
            "availableDecisions": ["accept", "decline"],
        },
    }


async def test_command_approval_becomes_inspectable_interaction(monkeypatch):
    adapter = _seed_adapter()
    await adapter._handle_approval_request(_command_request())

    interaction = adapter.list_pending_interactions("S-codex")[0]
    assert interaction.native_request_id == "41"
    assert interaction.native_session_id == "native-codex"
    assert interaction.payload["toolName"] == "shell"
    assert interaction.payload["toolView"]["command"].startswith("pytest")
    assert interaction.payload["toolView"]["paths"] == [
        "/workspace/tests/unit/test_app.py"]
    assert adapter._interaction_events["S-codex"].is_set()


async def test_command_rejection_declines_same_native_rpc(monkeypatch):
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    await adapter._handle_approval_request(_command_request())
    interaction = adapter.list_pending_interactions("S-codex")[0]
    result = await adapter.respond_interaction(
        "S-codex", interaction.interaction_id,
        {"outcome": "rejected"}, responded_by="hermes")

    assert result.state == "working"
    assert sent[-1] == {
        "jsonrpc": "2.0", "id": 41,
        "result": {"decision": "decline"},
    }
    assert not adapter.list_pending_interactions("S-codex")


async def test_command_allow_once_requires_bound_receipt(monkeypatch):
    monkeypatch.setenv("LAS_ACTION_RECEIPT_SECRET", "s" * 32)
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    await adapter._handle_approval_request(_command_request())
    interaction = adapter.list_pending_interactions("S-codex")[0]
    with pytest.raises(PermissionError, match="requires an approved"):
        await adapter.respond_interaction(
            "S-codex", interaction.interaction_id,
            {"outcome": "allowed-once"}, responded_by="user")

    from common.action_receipt import sign_action_receipt

    receipt = sign_action_receipt({
        "actionIntentId": "AI-1", "taskId": "T-codex",
        "interactionId": interaction.interaction_id,
        "nativeRequestId": "41", "nativeSessionId": "native-codex",
        "contextRevision": 4, "status": "approved", "decidedBy": "user",
    })
    await adapter.respond_interaction(
        "S-codex", interaction.interaction_id,
        {"outcome": "allowed-once", "authorization": receipt},
        responded_by="user")
    assert sent[-1]["result"] == {"decision": "accept"}


async def test_permissions_are_never_granted_beyond_current_turn(monkeypatch):
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    request = {
        "jsonrpc": "2.0", "id": 72,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": "native-codex", "turnId": "turn-1",
            "itemId": "item-9", "startedAtMs": 1, "cwd": "/workspace",
            "permissions": {"fileSystem": {
                "read": ["/workspace"], "write": ["/workspace/app.py"]},
                "network": {"enabled": False}},
        },
    }
    await adapter._handle_approval_request(request)
    interaction = adapter.list_pending_interactions("S-codex")[0]
    await adapter.respond_interaction(
        "S-codex", interaction.interaction_id,
        {"outcome": "rejected"}, responded_by="hermes")
    assert sent[-1]["result"] == {
        "permissions": {}, "scope": "turn", "strictAutoReview": True}


async def test_notifications_drop_command_output_and_reasoning():
    adapter = _seed_adapter()
    adapter._handle_notification("item/started", {
        "threadId": "native-codex", "turnId": "turn-1", "startedAtMs": 1,
        "item": {
            "id": "item-1", "type": "commandExecution",
            "command": "pytest", "commandActions": [], "cwd": "/workspace",
            "status": "inProgress", "aggregatedOutput": "private output",
        },
    })
    adapter._handle_notification("item/reasoning/textDelta", {
        "threadId": "native-codex", "turnId": "turn-1",
        "delta": "hidden chain of thought",
    })
    saved = adapter._updates["S-codex"][0]["item"]
    assert saved["command"] == "pytest"
    assert "aggregatedOutput" not in saved
    assert all("reasoning" not in item.get("method", "")
               for item in adapter._updates["S-codex"])


async def test_process_restart_resumes_read_only_thread(monkeypatch, tmp_path):
    adapter = _seed_adapter()
    calls = []

    async def connected():
        return None

    async def rpc(method, params):
        calls.append((method, params))
        return {"thread": {"id": "native-codex"}}

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_workspace", lambda task_id: tmp_path)
    await adapter._ensure_native_loaded("S-codex")
    await adapter._ensure_native_loaded("S-codex")

    assert [method for method, _ in calls] == ["thread/resume"]
    assert calls[0][1]["sandbox"] == "read-only"
    assert calls[0][1]["approvalPolicy"] == "on-request"
    assert calls[0][1]["approvalsReviewer"] == "user"


async def test_unknown_thread_approval_fails_closed(monkeypatch):
    adapter = _seed_adapter()
    sent = []

    async def capture(message):
        sent.append(message)

    monkeypatch.setattr(adapter, "_send_message", capture)
    request = _command_request()
    request["params"]["threadId"] = "unbound-thread"
    await adapter._handle_approval_request(request)
    assert sent[-1]["result"] == {"decision": "decline"}


async def test_allow_fails_closed_when_native_does_not_offer_accept(
        monkeypatch):
    monkeypatch.setenv("LAS_ACTION_RECEIPT_SECRET", "s" * 32)
    adapter = _seed_adapter()

    async def capture(message):
        return None

    monkeypatch.setattr(adapter, "_send_message", capture)
    request = _command_request()
    request["params"]["availableDecisions"] = ["decline"]
    await adapter._handle_approval_request(request)
    interaction = adapter.list_pending_interactions("S-codex")[0]
    from common.action_receipt import sign_action_receipt

    receipt = sign_action_receipt({
        "actionIntentId": "AI-2", "taskId": "T-codex",
        "interactionId": interaction.interaction_id,
        "nativeRequestId": "41", "nativeSessionId": "native-codex",
        "contextRevision": 4, "status": "approved", "decidedBy": "user",
    })
    with pytest.raises(SessionCapabilityError, match="one-shot accept"):
        await adapter.respond_interaction(
            "S-codex", interaction.interaction_id,
            {"outcome": "allowed-once", "authorization": receipt},
            responded_by="user")
