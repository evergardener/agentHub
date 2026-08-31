"""Codex App Server approval bridge and event translation tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from adapters.codex.runner import CodexFailed, CodexTimeout
from adapters.codex.session import CodexSessionAdapter
from adapters.common import A2aTask
from adapters.session import (
    SessionCapabilityError,
    SessionHandle,
    SessionMessage,
    SessionTurnResult,
)

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
    adapter._event_queues["S-codex"] = asyncio.Queue()
    return adapter


async def test_native_health_probe_checks_app_server_response(monkeypatch):
    adapter = CodexSessionAdapter(
        timeout_seconds=1, health_rpc_timeout_seconds=0.1)
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader

    async def connected():
        return None

    async def rpc(method, params):
        assert method == "model/list"
        assert params == {"includeHidden": False}
        return {"data": []}

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    try:
        assert await adapter.health() == {
            "runtime": "codex-app-server",
            "ready": True,
            "pendingRpcCount": 0,
        }
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_failed_health_probe_recycles_native_process(monkeypatch):
    adapter = CodexSessionAdapter(
        timeout_seconds=1, health_rpc_timeout_seconds=0.01)
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader
    recycled = []

    async def connected():
        return None

    async def rpc(method, params):
        await asyncio.Event().wait()

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    try:
        with pytest.raises(CodexTimeout, match="model/list exceeded"):
            await adapter.health()
        assert recycled and "failed health probe" in recycled[0]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_health_fails_closed_for_stalled_thread_start(monkeypatch):
    adapter = CodexSessionAdapter(
        timeout_seconds=10, health_rpc_timeout_seconds=0.01)
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader
    adapter._critical_rpc_started["thread/start"] = time.monotonic() - 1
    rpc_calls = []
    recycled = []

    async def connected():
        return None

    async def rpc(method, params):
        rpc_calls.append(method)
        return {"data": []}

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    try:
        with pytest.raises(CodexTimeout, match="thread/start is unresponsive"):
            await adapter.health()
        assert rpc_calls == []
        assert recycled and "thread/start is unresponsive" in recycled[0]
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_health_probe_does_not_recycle_active_turn(monkeypatch):
    adapter = CodexSessionAdapter(
        timeout_seconds=10, health_rpc_timeout_seconds=0.01)
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader
    adapter._active_turn_ids["S-codex"] = "turn-active"
    recycled = []

    async def connected():
        return None

    async def rpc(method, params):
        await asyncio.Event().wait()

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    try:
        with pytest.raises(CodexTimeout, match="model/list exceeded"):
            await adapter.health()
        assert recycled == []
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_repeated_health_failures_terminate_active_turn_and_recycle(
        monkeypatch):
    adapter = _seed_adapter()
    adapter.timeout_seconds = 10
    adapter.health_rpc_timeout_seconds = 0.01
    adapter.active_turn_health_failure_limit = 3
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader
    adapter._active_turn_ids["S-codex"] = "turn-active"
    turn = asyncio.get_running_loop().create_future()
    adapter._turn_futures["turn-active"] = turn
    waiter = asyncio.create_task(
        adapter._await_turn_or_interaction("S-codex"))
    recycled = []

    async def connected():
        return None

    async def rpc(method, params):
        await asyncio.Event().wait()

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    try:
        await asyncio.sleep(0)
        for _ in range(2):
            with pytest.raises(CodexTimeout, match="model/list exceeded"):
                await adapter.health()
            assert adapter._active_turn_ids == {"S-codex": "turn-active"}
            assert not turn.done()
            assert recycled == []

        with pytest.raises(CodexTimeout, match="model/list exceeded"):
            await adapter.health()
        assert recycled and "failed health probe" in recycled[0]
        assert adapter._active_turn_ids == {}
        assert adapter._turn_futures == {}
        assert turn.done()
        with pytest.raises(CodexFailed, match="active turn terminated"):
            await asyncio.wait_for(waiter, timeout=1)
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_successful_health_probe_resets_active_failure_count(monkeypatch):
    adapter = CodexSessionAdapter(
        timeout_seconds=1, health_rpc_timeout_seconds=0.01,
        active_turn_health_failure_limit=2)
    reader = asyncio.create_task(asyncio.sleep(10))
    adapter._reader_task = reader
    adapter._active_turn_ids["S-codex"] = "turn-active"
    recycled = []
    probe_count = 0

    async def connected():
        return None

    async def rpc(method, params):
        nonlocal probe_count
        probe_count += 1
        if probe_count == 2:
            return {"data": []}
        await asyncio.Event().wait()

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    try:
        with pytest.raises(CodexTimeout):
            await adapter.health()
        assert adapter._health_failure_count == 1
        assert await adapter.health() == {
            "runtime": "codex-app-server",
            "ready": True,
            "pendingRpcCount": 0,
        }
        assert adapter._health_failure_count == 0
        with pytest.raises(CodexTimeout):
            await adapter.health()
        assert adapter._health_failure_count == 1
        assert recycled == []
        assert adapter._active_turn_ids == {"S-codex": "turn-active"}
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def test_input_required_health_recovery_fails_task_and_queues_event():
    adapter = _seed_adapter()
    task = adapter._tasks["S-codex"]
    task.status_state = "input-required"
    await adapter._handle_approval_request(_command_request())
    interaction = adapter.list_pending_interactions("S-codex")[0]
    adapter._active_turn_ids["S-codex"] = "turn-active"
    turn = asyncio.get_running_loop().create_future()
    adapter._turn_futures["turn-active"] = turn

    adapter._fail_active_turns(
        CodexFailed("native Codex health checks failed repeatedly"))

    assert task.status_state == "failed"
    assert task.error == "native Codex health checks failed repeatedly"
    assert task.pending_interactions == []
    assert adapter.list_pending_interactions("S-codex") == []
    events = adapter.drain_recovery_events()
    assert len(events) == 1
    assert events[0].event_type == "task.failed"
    assert events[0].task_id == "T-codex"
    assert events[0].session_id == "S-codex"
    assert events[0].payload["reason"] == "native_runtime_unavailable"
    assert events[0].payload["status_from"] == "blocked"
    assert events[0].payload["interaction_ids"] == [
        interaction.interaction_id]
    with pytest.raises(CodexFailed):
        turn.result()


async def test_cancelled_rpc_does_not_leak_pending_request(monkeypatch):
    adapter = CodexSessionAdapter(timeout_seconds=10)

    async def connected():
        return None

    async def sent(message):
        return None

    monkeypatch.setattr(adapter, "_ensure_process_stream", connected)
    monkeypatch.setattr(adapter, "_send_message", sent)
    pending = asyncio.create_task(adapter._rpc("thread/start", {}))
    while not adapter._pending_rpc:
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert adapter._pending_rpc == {}


async def test_rpc_send_failure_does_not_leak_pending_request(monkeypatch):
    adapter = CodexSessionAdapter(timeout_seconds=10)

    async def connected():
        return None

    async def broken_send(message):
        raise BrokenPipeError("native stdin closed")

    monkeypatch.setattr(adapter, "_ensure_process_stream", connected)
    monkeypatch.setattr(adapter, "_send_message", broken_send)
    with pytest.raises(BrokenPipeError, match="native stdin closed"):
        await adapter._rpc("thread/start", {})
    assert adapter._pending_rpc == {}


async def test_session_start_timeout_recycles_app_server(
        monkeypatch, tmp_path):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    adapter = CodexSessionAdapter(
        timeout_seconds=10, startup_rpc_timeout_seconds=0.01)
    recycled = []

    async def connected():
        return None

    async def rpc(method, params):
        assert method == "thread/start"
        await asyncio.Event().wait()

    async def close(error):
        recycled.append(str(error))

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_close_process_unlocked", close)
    task = A2aTask(
        id="T-timeout", status_state="working", objective="validate",
        session_id="S-timeout", context_revision=1)
    with pytest.raises(CodexTimeout, match="thread/start exceeded"):
        await adapter.start_session(
            task, session_id="S-timeout",
            metadata={"executionWorkspace": str(execution_workspace)})
    assert recycled == [
        "Codex App Server recycled after session startup timeout"]


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


async def test_file_change_approval_preserves_native_change_targets():
    adapter = _seed_adapter()
    adapter._items[("native-codex", "item-edit")] = {
        "id": "item-edit", "type": "fileChange", "status": "inProgress",
        "changes": [{
            "path": "/workspace/src/app.py",
            "kind": {"type": "update"},
            "diff": "@@ -1 +1 @@\n-old\n+new",
        }],
    }

    await adapter._handle_approval_request({
        "jsonrpc": "2.0", "id": 51,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": "native-codex", "turnId": "turn-1",
            "itemId": "item-edit", "startedAtMs": 1,
            "reason": "apply patch",
        },
    })

    interaction = adapter.list_pending_interactions("S-codex")[0]
    assert interaction.payload["toolView"]["paths"] == [
        "/workspace/src/app.py"]
    assert interaction.payload["toolView"]["changes"][0]["kind"] == {
        "type": "update"}


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


async def test_failed_native_approval_delivery_remains_retryable(monkeypatch):
    adapter = _seed_adapter()

    async def broken_send(message):
        raise BrokenPipeError("native approval channel closed")

    monkeypatch.setattr(adapter, "_send_message", broken_send)
    await adapter._handle_approval_request(_command_request())
    interaction = adapter.list_pending_interactions("S-codex")[0]
    with pytest.raises(BrokenPipeError, match="approval channel closed"):
        await adapter.respond_interaction(
            "S-codex", interaction.interaction_id,
            {"outcome": "rejected"}, responded_by="hermes")

    assert interaction.status == "pending"
    assert interaction.interaction_id in adapter._approval_methods
    assert interaction.interaction_id in adapter._approval_params
    assert interaction.interaction_id in adapter._approval_rpc_ids


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
    streamed = await anext(adapter.stream_events("S-codex"))
    assert streamed.event_type == "item.lifecycle"
    assert "aggregatedOutput" not in streamed.payload["item"]


async def test_process_restart_resumes_read_only_thread(monkeypatch, tmp_path):
    adapter = _seed_adapter()
    calls = []

    async def connected():
        return None

    async def rpc(method, params):
        calls.append((method, params))
        return {"thread": {"id": "native-codex"}, "cwd": str(tmp_path)}

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_workspace", lambda task_id: tmp_path)
    await adapter._ensure_native_loaded("S-codex")
    await adapter._ensure_native_loaded("S-codex")

    assert [method for method, _ in calls] == ["thread/resume"]
    assert calls[0][1]["sandbox"] == "read-only"
    assert calls[0][1]["approvalPolicy"] == "on-request"
    assert calls[0][1]["approvalsReviewer"] == "user"


async def test_explicit_workspace_is_used_for_start_and_resume(
        monkeypatch, tmp_path):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    adapter = CodexSessionAdapter(timeout_seconds=1)
    calls = []

    async def connected():
        return None

    async def rpc(method, params):
        calls.append((method, params))
        return {
            "thread": {"id": "native-explicit"},
            "cwd": str(execution_workspace.resolve()),
        }

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    task = A2aTask(
        id="T-explicit", status_state="working", objective="edit project",
        session_id="S-explicit", context_revision=1,
    )
    await adapter.start_session(
        task, session_id="S-explicit",
        metadata={"executionWorkspace": str(execution_workspace)},
    )
    adapter._loaded_threads.clear()
    await adapter._ensure_native_loaded("S-explicit")

    assert [method for method, _ in calls] == ["thread/start", "thread/resume"]
    for _, params in calls:
        assert params["cwd"] == str(execution_workspace.resolve())
        assert params["runtimeWorkspaceRoots"] == [
            str(execution_workspace.resolve())]
        assert params["sandbox"] == "read-only"


async def test_task_runtime_config_is_validated_and_applied(
        monkeypatch, tmp_path):
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    adapter = CodexSessionAdapter(timeout_seconds=1)
    calls = []

    async def connected():
        return None

    async def rpc(method, params):
        calls.append((method, params))
        if method == "model/list":
            return {"data": [{
                "model": "gpt-5.6-luna", "isDefault": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": ""},
                    {"reasoningEffort": "max", "description": ""},
                ],
            }], "nextCursor": None}
        if method == "thread/start":
            return {
                "thread": {"id": "native-runtime"},
                "cwd": str(execution_workspace.resolve()),
                "model": "gpt-5.6-luna", "reasoningEffort": "max",
            }
        raise AssertionError(method)

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    task = A2aTask(
        id="T-runtime", status_state="working", objective="validate",
        session_id="S-runtime", context_revision=1,
    )
    await adapter.start_session(
        task, session_id="S-runtime", metadata={
            "executionWorkspace": str(execution_workspace),
            "model": "gpt-5.6-luna", "reasoningEffort": "max",
        })

    assert [method for method, _ in calls] == ["model/list", "thread/start"]
    params = calls[-1][1]
    assert params["model"] == "gpt-5.6-luna"
    assert params["config"]["model_reasoning_effort"] == "max"
    assert adapter._session_models["S-runtime"] == "gpt-5.6-luna"
    assert adapter._session_reasoning_efforts["S-runtime"] == "max"


async def test_task_runtime_config_rejects_unsupported_model_or_effort(
        monkeypatch, tmp_path):
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    adapter = CodexSessionAdapter(timeout_seconds=1)

    async def connected():
        return None

    async def rpc(method, params):
        assert method == "model/list"
        return {"data": [{
            "model": "gpt-5.6-terra", "isDefault": True,
            "supportedReasoningEfforts": [{
                "reasoningEffort": "medium", "description": ""}],
        }], "nextCursor": None}

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    task = A2aTask(
        id="T-runtime-invalid", status_state="working", objective="validate",
        session_id="S-runtime-invalid", context_revision=1,
    )
    with pytest.raises(CodexFailed, match="unsupported model"):
        await adapter.start_session(
            task, session_id="S-runtime-invalid", metadata={
                "executionWorkspace": str(execution_workspace),
                "model": "gpt-5.6-luna"})
    with pytest.raises(CodexFailed, match="unsupported reasoning effort"):
        await adapter.start_session(
            task, session_id="S-runtime-invalid", metadata={
                "executionWorkspace": str(execution_workspace),
                "model": "gpt-5.6-terra", "reasoningEffort": "max"})


def test_thread_result_must_match_requested_runtime_config(tmp_path):
    with pytest.raises(CodexFailed, match="model does not match"):
        CodexSessionAdapter._verify_thread_result(
            {"thread": {"id": "native"}, "cwd": str(tmp_path),
             "model": "gpt-5.6-terra", "reasoningEffort": "max"},
            expected_workspace=tmp_path,
            expected_model="gpt-5.6-luna", expected_reasoning_effort="max")
    with pytest.raises(CodexFailed, match="reasoning effort does not match"):
        CodexSessionAdapter._verify_thread_result(
            {"thread": {"id": "native"}, "cwd": str(tmp_path),
             "model": "gpt-5.6-luna", "reasoningEffort": "medium"},
            expected_workspace=tmp_path,
            expected_model="gpt-5.6-luna", expected_reasoning_effort="max")


async def test_task_runtime_config_is_reapplied_to_each_turn(
        monkeypatch, tmp_path):
    adapter = _seed_adapter()
    adapter._session_models["S-codex"] = "gpt-5.6-luna"
    adapter._session_reasoning_efforts["S-codex"] = "max"
    calls = []
    control_workspace = tmp_path / "control"
    control_workspace.mkdir()

    async def loaded(session_id):
        return None

    async def rpc(method, params):
        calls.append((method, params))
        return {"turn": {"id": "turn-runtime"}}

    async def completed(session_id):
        return SessionTurnResult(state="completed")

    monkeypatch.setattr(adapter, "_ensure_native_loaded", loaded)
    monkeypatch.setattr(adapter, "_rpc", rpc)
    monkeypatch.setattr(adapter, "_await_turn_or_interaction", completed)
    monkeypatch.setattr(
        adapter, "_workspace", lambda task_id: control_workspace)
    result = await adapter.send_message(
        "S-codex", SessionMessage(
            message_id="M-runtime", role="user", content="validate"))

    assert result.state == "completed"
    assert calls[0][0] == "turn/start"
    assert calls[0][1]["model"] == "gpt-5.6-luna"
    assert calls[0][1]["effort"] == "max"


async def test_explicit_workspace_must_exist_and_be_a_directory(
        monkeypatch, tmp_path):
    adapter = CodexSessionAdapter(timeout_seconds=1)

    async def connected():
        return None

    monkeypatch.setattr(adapter, "_ensure_connected", connected)
    task = A2aTask(
        id="T-invalid", status_state="working", objective="edit project",
        session_id="S-invalid", context_revision=1,
    )
    with pytest.raises(CodexFailed, match="unavailable"):
        await adapter.start_session(
            task, session_id="S-invalid",
            metadata={"executionWorkspace": str(tmp_path / "missing")},
        )
    with pytest.raises(CodexFailed, match="absolute"):
        await adapter.start_session(
            task, session_id="S-invalid",
            metadata={"executionWorkspace": "relative/project"},
        )


def test_explicit_workspace_artifacts_include_only_changed_files(
        monkeypatch, tmp_path):
    control_workspace = tmp_path / "agenthub"
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    changed = execution_workspace / "src" / "changed.py"
    changed.parent.mkdir()
    changed.write_text("changed = True\n", encoding="utf-8")
    (execution_workspace / "untouched.py").write_text(
        "untouched = True\n", encoding="utf-8")
    monkeypatch.setenv("LAS_WORKSPACE", str(control_workspace))
    adapter = _seed_adapter()
    adapter._session_workspaces["S-codex"] = execution_workspace.resolve()
    adapter._explicit_workspace_sessions.add("S-codex")
    adapter._updates["S-codex"] = [{
        "method": "item/completed",
        "item": {
            "id": "file-1", "type": "fileChange", "status": "completed",
            "changes": [{"path": str(changed),
                         "kind": {"type": "update"}, "diff": "@@"}],
        },
    }]

    artifacts = adapter._collect_turn_artifacts("S-codex")
    names = {item["name"] for item in artifacts}

    assert "workspace/src/changed.py" in names
    assert "workspace/untouched.py" not in names
    assert not (execution_workspace / "context.md").exists()


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


async def test_codex_steer_targets_expected_active_turn(monkeypatch):
    adapter = _seed_adapter()
    adapter._active_turn_ids["S-codex"] = "turn-active"
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        return {"turnId": "turn-active"}

    monkeypatch.setattr(adapter, "_rpc", rpc)
    from adapters.session import SessionMessage

    handle = await adapter.steer("S-codex", SessionMessage(
        message_id="M-steer", role="user", content="只改 API",
        based_on_revision=5))
    assert calls == [("turn/steer", {
        "threadId": "native-codex", "expectedTurnId": "turn-active",
        "input": [{"type": "text", "text": "只改 API"}],
        "clientUserMessageId": "M-steer",
    })]
    assert handle.context_revision == 5
