"""DSH Web API session protocol and durable resume tests."""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.common import A2aTask
from adapters.dsh.session import DshWebSessionAdapter
from adapters.session import SessionMessage

pytestmark = pytest.mark.anyio


class DshFixture:
    def __init__(self, *, approval: bool = False):
        self.approval = approval
        self.session_id = "session-native-dsh-1"
        self.events: list[dict] = []
        self.methods: list[str] = []
        self.prompts: list[str] = []
        self.next_seq = 0
        self.responses: list[dict] = []

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
            self._event("assistant/message", {
                "turn": turn, "step": turn,
                "message": {
                    "id": f"assistant-{turn}", "role": "assistant",
                    "content": [{"type": "text", "text": f"done: {prompt}"}],
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
            value = {"sessionId": self.session_id}
        elif method == "session.list":
            value = {"items": [{"sessionId": self.session_id}]}
        elif method == "session.history":
            value = {"events": self.events, "hasMore": False}
        elif method == "session.prompt":
            prompt = body["payload"]["content"][0]["text"]
            if prompt.startswith("/permission "):
                value = {"accepted": True, "command": {"kind": "success"}}
                return httpx.Response(200, json={
                    "type": "server-response",
                    "rpcId": body["rpcId"],
                    "result": {"ok": True, "value": value},
                })
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
        interaction_wait_seconds=0), client


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
        assert fixture.methods.count("session.prompt") == 3
        assert fixture.prompts == ["first", "second"]
        assert adapter.get_session("S-dsh").native_session_id == \
            fixture.session_id
        assert any(a["name"] == "last-message.md" for a in second.artifacts)
        assert adapter.capabilities.native_resume is True
        assert adapter.capabilities.pause is False
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
        assert fixture.methods == ["session.history"]
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
                    "cwd": str(tmp_path)}},
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
        accepted = await adapter.respond_interaction(
            "S-dsh", pending[0].interaction_id,
            {"outcome": "allowed-once", "authorization": authorization},
            responded_by="hermes",
        )
        assert accepted.state == "working"
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
        "card": "terminal", "title": "pwd", "cwd": str(tmp_path)}}
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
            "runtime": "dsh-web", "sessions": 1}
        assert fixture.methods == ["session.list"]
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
