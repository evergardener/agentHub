"""DSH Web API session protocol and durable resume tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from adapters.common import A2aTask
from adapters.dsh.session import (
    DshApiError,
    DshNotAvailable,
    DshWebSessionAdapter,
    MAX_DSH_HISTORY_MESSAGES,
)
from adapters.session import SessionCapabilityError, SessionMessage

pytestmark = pytest.mark.anyio


class DshFixture:
    def __init__(self, *, approval: bool = False,
                 answer: str | None = None, extra_chunks: int = 0):
        self.approval = approval
        self.answer = answer
        self.extra_chunks = extra_chunks
        self.session_id = "session-native-dsh-1"
        self.events: list[dict] = []
        self.methods: list[str] = []
        self.prompts: list[str] = []
        self.next_seq = 0
        self.responses: list[dict] = []
        self.workspace_id = "workspace-native-dsh-1"
        self.workspace_path: str | None = None
        self.workspace_sessions: list[str] = []
        self.session_create_payloads: list[dict] = []
        self.workspace_create_payloads: list[dict] = []

    def _event(self, event_type: str, data: dict) -> dict:
        seq = self.next_seq
        self.next_seq += 1
        return {"event": {
            "seq": seq,
            "time": 1_786_000_000_000 + seq,
            "type": event_type,
            "data": data,
        }}

    def _complete_turn(self, prompt: str) -> None:
        turn = len(self.prompts)
        self.events.extend([
            self._event("turn/start", {"turn": turn}),
            self._event("user/message", {
                "id": f"user-{turn}", "role": "user",
                "content": [{"type": "text", "text": prompt}],
                "source": {"kind": "user"},
            }),
        ])
        self.events.extend(
            self._event("assistant/chunk", {
                "turn": turn, "step": turn,
                "chunk": {"type": "reasoning-delta", "index": 0,
                          "text": f"chunk-{index}"},
            }) for index in range(self.extra_chunks)
        )
        self.events.extend([
            self._event("assistant/message", {
                "turn": turn, "step": turn,
                "message": {
                    "id": f"assistant-{turn}", "role": "assistant",
                    "content": [{"type": "text", "text":
                                 self.answer or f"done: {prompt}"}],
                    "source": {"kind": "model", "provider": "test",
                               "model": "test"},
                },
            }),
            self._event("turn/end", {
                "turn": turn, "reason": {"kind": "completed"},
            }),
        ])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/respond":
            self.responses.append(body)
            value = body["result"]["value"]
            self.events.extend([
                self._event("approval/decided", {
                    "id": value["approvalId"],
                    "outcome": value["outcome"],
                }),
                self._event("turn/end", {
                    "turn": 1, "reason": {"kind": "completed"},
                }),
            ])
            return httpx.Response(200, json={"accepted": True})
        method = body["method"]
        self.methods.append(method)
        value: dict = {}
        if method == "session.create":
            assert body["payload"]["agentPreset"] == "standard"
            self.session_create_payloads.append(dict(body["payload"]))
            if body["payload"].get("workspaceId") == self.workspace_id:
                self.workspace_sessions = [self.session_id]
            value = {"sessionId": self.session_id}
        elif method == "session.list":
            cwd = self.workspace_path or str(
                Path(os.environ.get("LAS_WORKSPACE", "/tmp"))
                / "tasks" / "T-dsh")
            value = {"items": [{
                "sessionId": self.session_id,
                "agentPreset": "standard",
                "cwd": cwd,
            }]}
        elif method == "workspace.create":
            self.workspace_create_payloads.append(dict(body["payload"]))
            self.workspace_path = body["payload"]["path"]
            value = {"workspace": {
                "workspaceId": self.workspace_id,
                "path": self.workspace_path,
                "title": Path(self.workspace_path).name,
                "sessionIds": list(self.workspace_sessions),
                "createdAt": "2026-08-23T00:00:00Z",
                "updatedAt": "2026-08-23T00:00:00Z",
            }, "created": True}
        elif method == "workspace.list":
            value = {"items": ([{
                "workspaceId": self.workspace_id,
                "path": self.workspace_path,
                "title": Path(self.workspace_path).name,
                "sessionIds": list(self.workspace_sessions),
                "createdAt": "2026-08-23T00:00:00Z",
                "updatedAt": "2026-08-23T00:00:00Z",
            }] if self.workspace_path else []), "archivedSessionIds": []}
        elif method == "session.history":
            assert body["payload"]["maxMessages"] == \
                MAX_DSH_HISTORY_MESSAGES
            value = {"events": self.events, "hasMore": False}
        elif method == "commands/execute":
            args = body["payload"]["args"]
            assert args == {
                "agentId": self.session_id,
                "line": "/permission read-only",
            }
            self.events.extend([
                self._event("permission/preset", {"preset": "read-only"}),
                self._event("sandbox/mode", {"mode": "read-only"}),
                self._event("approval/policy", {"policy": "ask"}),
            ])
            value = {
                "commandId": "command-permission",
                "result": {"kind": "success", "text": "preset read-only"},
            }
        elif method == "session.prompt":
            prompt = body["payload"]["content"][0]["text"]
            self.prompts.append(prompt)
            if self.approval:
                self.events.extend([
                    self._event("turn/start", {"turn": 1}),
                    self._event("approval/asked", {
                        "id": "approval-1", "toolName": "bash",
                        "reason": "modify workspace",
                    }),
                ])
            else:
                self._complete_turn(prompt)
            value = {"accepted": True}
        elif method == "session.cancel":
            value = {"accepted": True}
        else:
            raise AssertionError(f"unexpected DSH method: {method}")
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": value},
        })


def _task(native: str | None = None) -> A2aTask:
    return A2aTask(
        id="T-dsh", status_state="submitted", objective="review",
        session_id="S-dsh", native_session_id=native,
    )


def _adapter(fixture: DshFixture) -> tuple[DshWebSessionAdapter,
                                            httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fixture), base_url="http://dsh.test")
    return DshWebSessionAdapter(
        client=client, poll_interval=0, timeout_seconds=1,
        interaction_wait_seconds=0, allow_unverified_runtime=True,
        production_mode=False), client


async def test_dsh_uses_same_native_session_for_multiple_turns(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        handle = await adapter.start_session(
            _task(), session_id="S-dsh", metadata={})
        assert handle.native_session_id == fixture.session_id
        first = await adapter.send_message(
            "S-dsh", SessionMessage("M-1", "user", "first"))
        second = await adapter.send_message(
            "S-dsh", SessionMessage("M-2", "user", "second"))
        assert first.state == second.state == "completed"
        assert fixture.methods.count("session.create") == 1
        assert fixture.methods.count("session.prompt") == 2
        assert fixture.prompts == ["first", "second"]
        assert adapter.get_session("S-dsh").native_session_id == \
            fixture.session_id
        assert any(a["name"] == "last-message.md" for a in second.artifacts)
        assert adapter.capabilities.native_resume is True
        assert adapter.capabilities.pause is False
    finally:
        await client.aclose()


async def test_dsh_registers_explicit_workspace_and_accounts_session(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        handle = await adapter.start_session(
            _task(), session_id="S-dsh",
            metadata={"executionWorkspace": str(execution_workspace)})
        assert handle.native_session_id == fixture.session_id
        assert fixture.workspace_create_payloads == [
            {"path": str(execution_workspace.resolve())}]
        assert fixture.session_create_payloads == [{
            "workspaceId": fixture.workspace_id,
            "agentPreset": "standard",
        }]
        assert fixture.session_id in fixture.workspace_sessions
        assert "workspace.create" in fixture.methods
        assert "workspace.list" in fixture.methods
    finally:
        await client.aclose()


async def test_dsh_approval_is_scoped_to_explicit_workspace(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(
            _task(), session_id="S-dsh",
            metadata={"executionWorkspace": str(execution_workspace)})
        await adapter.ingest_server_request({
            "type": "server-request", "rpcId": "rpc-tool-workspace",
            "method": "session/event", "payload": {
                "type": "session/event", "sessionId": fixture.session_id,
                "event": {"type": "tool/call", "data": {
                    "callId": "call-workspace", "name": "bash"}},
                "view": {"for": "call", "view": {
                    "card": "terminal", "title": "touch safe.txt",
                    "cwd": str(execution_workspace)}},
            },
        })
        await adapter.ingest_server_request({
            "type": "server-request", "rpcId": "rpc-approval-workspace",
            "method": "approval/requested", "payload": {
                "type": "approval/requested",
                "sessionId": fixture.session_id,
                "approvalId": "approval-workspace",
                "toolName": "bash", "callId": "call-workspace",
            },
        })

        pending = adapter.list_pending_interactions("S-dsh")[0]
        assert pending.payload["inspectable"] is True
        assert pending.payload["toolView"]["semanticIntent"]["targets"][
            "workspace"] == str(execution_workspace.resolve())
    finally:
        await client.aclose()


async def test_dsh_preserves_long_redacted_answer_and_more_than_200_events(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    answer = "结果开始\n" + "容器信息\n" * 2500 + "token=secret-value\n结果结束"
    fixture = DshFixture(answer=answer, extra_chunks=240)
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        result = await adapter.send_message(
            "S-dsh", SessionMessage("M-long", "user", "inspect"))
        by_name = {item["name"]: Path(item["path"])
                   for item in result.artifacts}
        report = by_name["last-message.md"].read_text(encoding="utf-8")
        assert report.startswith("结果开始")
        assert report.endswith("结果结束")
        assert "token=[REDACTED]" in report
        assert "secret-value" not in report
        assert len(report) > 8192

        history = json.loads(
            by_name["dsh-history.json"].read_text(encoding="utf-8"))
        assert history["totalEvents"] == len(fixture.events)
        assert history["retainedEvents"] == len(fixture.events)
        assert history["eventTruncated"] is False
        assert history["fieldTruncated"] is False
        assert history["truncated"] is False
        assert len(history["entries"]) > 200
    finally:
        await client.aclose()


async def test_dsh_steer_uses_native_steer_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        calls_before = len(fixture.prompts)
        handle = await adapter.steer(
            "S-dsh", SessionMessage(
                "M-steer", "user", "只复审 API", based_on_revision=2))
        assert fixture.prompts[calls_before:] == ["只复审 API"]
        assert handle.context_revision == 2
        streamed = await anext(adapter.stream_events("S-dsh"))
        assert streamed.event_type == "user.steer"
    finally:
        await client.aclose()


async def test_dsh_adapter_restart_validates_and_resumes_native_session(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        handle = await adapter.start_session(
            _task(fixture.session_id), session_id="S-restored",
            metadata={"nativeSessionId": fixture.session_id})
        assert handle.native_session_id == fixture.session_id
        assert "session.create" not in fixture.methods
        assert fixture.methods == [
            "session.history", "session.list", "commands/execute",
            "session.history"]
        await adapter.resume_session("S-restored")
        assert fixture.methods[-1] == "session.history"
    finally:
        await client.aclose()


async def test_dsh_pending_native_approval_becomes_input_required(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture(approval=True)
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        result = await adapter.send_message(
            "S-dsh", SessionMessage("M-1", "user", "modify"))
        assert result.state == "input-required"
        assert any(e.event_type == "approval.requested" for e in result.events)
        await adapter.interrupt("S-dsh")
        assert adapter.get_session("S-dsh").status == "paused"
        assert fixture.methods[-1] == "session.cancel"
    finally:
        await client.aclose()


async def test_dsh_native_approval_requires_action_intent_receipt(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "test-secret-0123456789abcdef")
    fixture = DshFixture(approval=True)
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        await adapter.ingest_server_request({
            "type": "server-request", "rpcId": "rpc-tool-1",
            "method": "session/event", "payload": {
                "type": "session/event", "sessionId": fixture.session_id,
                "event": {"type": "tool/call", "data": {
                    "callId": "call-1", "name": "bash",
                    "arguments": "hidden"}},
                "view": {"for": "call", "view": {
                    "card": "terminal", "title": "touch safe.txt",
                    "cwd": str(tmp_path / "tasks" / "T-dsh")}},
            },
        })
        await adapter.ingest_server_request({
            "type": "server-request",
            "rpcId": "rpc-approval-1",
            "method": "approval/requested",
            "payload": {
                "type": "approval/requested",
                "sessionId": fixture.session_id,
                "approvalId": "approval-1",
                "toolName": "bash",
                "callId": "call-1",
                "reason": "modify workspace",
            },
        })
        turn = await adapter.send_message(
            "S-dsh", SessionMessage("M-1", "user", "modify"))
        assert turn.state == "input-required"
        pending = adapter.list_pending_interactions("S-dsh")
        assert pending[0].native_request_id == "rpc-approval-1"
        assert pending[0].payload["toolView"]["command"] == "touch safe.txt"

        with pytest.raises(PermissionError, match="ActionIntent"):
            await adapter.respond_interaction(
                "S-dsh", pending[0].interaction_id,
                {"outcome": "allowed-once"}, responded_by="hermes")

        from common.action_receipt import sign_action_receipt

        authorization = sign_action_receipt({
            "actionIntentId": "AI-1", "status": "approved",
            "decidedBy": "hermes", "decidedAt": "now",
            "basedOnRevision": 1, "taskId": "T-dsh",
            "interactionId": pending[0].interaction_id,
            "nativeRequestId": "rpc-approval-1",
        })
        response = {"outcome": "allowed-once",
                    "authorization": authorization}
        original_post = adapter._post_response
        post_calls = 0

        async def accepted_but_response_lost(native_request_id, value):
            nonlocal post_calls
            post_calls += 1
            await original_post(native_request_id, value)
            raise DshNotAvailable("injected response loss after native accept")

        adapter._post_response = accepted_but_response_lost
        with pytest.raises(DshNotAvailable, match="response loss"):
            await adapter.respond_interaction(
                "S-dsh", pending[0].interaction_id, response,
                responded_by="hermes")

        # History now contains approval/decided. Retry reconciles that exact
        # outcome and must not send a second /api/respond.
        accepted = await adapter.respond_interaction(
            "S-dsh", pending[0].interaction_id, response,
            responded_by="hermes")
        assert accepted.state == "working"
        duplicate = await adapter.respond_interaction(
            "S-dsh", pending[0].interaction_id, response,
            responded_by="hermes")
        assert duplicate.state == "working"
        with pytest.raises(SessionCapabilityError, match="different response"):
            await adapter.respond_interaction(
                "S-dsh", pending[0].interaction_id,
                {"outcome": "rejected"}, responded_by="hermes")
        assert post_calls == 1
        assert len(fixture.responses) == 1
        completed = await adapter.continue_after_interaction("S-dsh")
        assert completed.state == "completed"
        assert fixture.responses[0]["rpcId"] == "rpc-approval-1"
        assert fixture.responses[0]["result"]["value"] == {
            "sessionId": fixture.session_id,
            "approvalId": "approval-1",
            "outcome": "allowed-once",
        }
    finally:
        await client.aclose()


async def test_dsh_event_stream_loop_reconnects_after_disconnect(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        calls = 0

        async def flaky_stream():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("injected SSE disconnect")
            adapter._event_stream_stop.set()

        adapter._consume_event_stream = flaky_stream
        await asyncio.wait_for(adapter._event_stream_loop(), timeout=1)
        assert calls == 2
        event = await asyncio.wait_for(
            anext(adapter.stream_events("S-dsh")), timeout=1)
        assert event.event_type == "dsh.stream.disconnected"
        assert "injected SSE disconnect" in event.payload["error"]
    finally:
        await client.aclose()


async def test_dsh_uninspectable_approval_can_only_be_rejected(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture(approval=True)
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        await adapter.ingest_server_request({
            "type": "server-request", "rpcId": "rpc-blind",
            "method": "approval/requested", "payload": {
                "type": "approval/requested",
                "sessionId": fixture.session_id,
                "approvalId": "approval-1", "toolName": "bash",
                "callId": "missing-call-view",
            },
        })
        await adapter.send_message(
            "S-dsh", SessionMessage("M-1", "user", "modify"))
        pending = adapter.list_pending_interactions("S-dsh")[0]
        with pytest.raises(PermissionError, match="details are incomplete"):
            await adapter.respond_interaction(
                "S-dsh", pending.interaction_id,
                {"outcome": "allowed-once", "authorization": {
                    "actionIntentId": "AI-1", "status": "approved",
                    "decidedBy": "user",
                }}, responded_by="user")
        accepted = await adapter.respond_interaction(
            "S-dsh", pending.interaction_id,
            {"outcome": "rejected"}, responded_by="user")
        assert accepted.state == "working"
    finally:
        await client.aclose()


async def test_dsh_replayed_approval_recovers_safe_view_from_history(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    call = fixture._event("tool/call", {
        "turn": 1, "step": 1, "callId": "call-replayed",
        "name": "bash", "arguments": "hidden",
    })
    call["view"] = {"for": "call", "view": {
        "card": "terminal", "title": "pwd",
        "cwd": str(tmp_path / "tasks" / "T-dsh")}}
    fixture.events.append(call)
    adapter, client = _adapter(fixture)
    try:
        await adapter.start_session(
            _task(fixture.session_id), session_id="S-restored", metadata={})
        await adapter.ingest_server_request({
            "type": "server-request", "rpcId": "rpc-replayed",
            "method": "approval/requested", "payload": {
                "type": "approval/requested",
                "sessionId": fixture.session_id,
                "approvalId": "approval-replayed", "toolName": "bash",
                "callId": "call-replayed",
            },
        })
        pending = adapter.list_pending_interactions("S-restored")[0]
        assert pending.payload["inspectable"] is True
        assert pending.payload["toolView"]["command"] == "pwd"
    finally:
        await client.aclose()


async def test_dsh_health_checks_the_native_runtime():
    fixture = DshFixture()
    adapter, client = _adapter(fixture)
    try:
        assert await adapter.health() == {
            "runtime": "dsh-web", "sessions": 1,
            "ready": True, "nativePermissionEnforcement": True,
            "permissionPreset": "read-only", "approvalPolicy": "ask",
            "agentPreset": "standard",
            "modelPromptsEnabled": True,
        }
        assert fixture.methods == ["session.list"]
    finally:
        await client.aclose()


async def test_dsh_model_prompt_requires_verified_native_permission(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("LAS_DSH_ALLOW_UNVERIFIED_RUNTIME", raising=False)
    fixture = DshFixture()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fixture), base_url="http://dsh.test")
    adapter = DshWebSessionAdapter(client=client, event_stream=False)
    try:
        await adapter.start_session(_task(), session_id="S-dsh", metadata={})
        health = await adapter.health()
        assert health["ready"] is True
        assert health["nativePermissionEnforcement"] is True
        result = await adapter.send_message(
            "S-dsh", SessionMessage("M-1", "user", "review"))
        assert result.state == "completed"
        assert fixture.methods.index("commands/execute") < \
            fixture.methods.index("session.prompt")
    finally:
        await client.aclose()


async def test_dsh_fails_closed_when_permission_projection_is_incomplete(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    original = fixture.__call__

    async def missing_policy(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if json.loads(request.content)["method"] == "commands/execute":
            fixture.events = [
                entry for entry in fixture.events
                if entry["event"]["type"] != "approval/policy"
            ]
        return response

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(missing_policy),
        base_url="http://dsh.test")
    adapter = DshWebSessionAdapter(client=client, event_stream=False)
    try:
        with pytest.raises(DshApiError, match="permission verification failed"):
            await adapter.start_session(
                _task(), session_id="S-dsh", metadata={})
        assert "session.prompt" not in fixture.methods
    finally:
        await client.aclose()


async def test_dsh_rejects_unaudited_agent_preset_before_creation(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    fixture = DshFixture()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fixture), base_url="http://dsh.test")
    adapter = DshWebSessionAdapter(client=client, event_stream=False)
    try:
        with pytest.raises(DshApiError, match="audited standard preset"):
            await adapter.start_session(
                _task(), session_id="S-dsh",
                metadata={"dshAgentPreset": "minimal"})
        assert fixture.methods == []
    finally:
        await client.aclose()


async def test_dsh_development_override_cannot_start_in_production():
    fixture = DshFixture()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fixture), base_url="http://dsh.test")
    try:
        with pytest.raises(ValueError, match="LAS_PRODUCTION_MODE is true"):
            DshWebSessionAdapter(
                client=client, allow_unverified_runtime=True,
                production_mode=True)
    finally:
        await client.aclose()


@pytest.mark.parametrize("preset", ["workspace-write", "danger-full-access"])
async def test_dsh_rejects_uncontrolled_write_access(preset):
    fixture = DshFixture()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(fixture), base_url="http://dsh.test")
    try:
        with pytest.raises(ValueError, match="must remain read-only"):
            DshWebSessionAdapter(
                client=client, permission_preset=preset)
    finally:
        await client.aclose()
