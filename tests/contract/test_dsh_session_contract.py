"""A2A surface contract for the DSH native-session adapter."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from adapters.dsh.card import agent_card
from adapters.dsh.session import DshWebSessionAdapter
from adapters.server_common import build_app

pytestmark = pytest.mark.anyio


async def test_dsh_a2a_card_and_native_session_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    events: list[dict] = []

    async def dsh(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        value: dict
        if method == "session.create":
            value = {"sessionId": "session-contract-dsh"}
        elif method == "session.history":
            value = {"events": events, "hasMore": False}
        elif method == "session.prompt":
            prompt = body["payload"]["content"][0]["text"]
            if prompt.startswith("/permission "):
                value = {"accepted": True, "command": {"kind": "success"}}
            else:
                events.extend([
                {"event": {"seq": 0, "time": 1, "type": "turn/start",
                           "data": {"turn": 1}}},
                {"event": {"seq": 1, "time": 2,
                           "type": "assistant/message", "data": {
                               "turn": 1, "step": 1, "message": {
                                   "id": "a", "role": "assistant",
                                   "content": [{"type": "text", "text": "ok"}],
                                   "source": {"kind": "model", "provider": "x",
                                              "model": "x"}}}}},
                {"event": {"seq": 2, "time": 3, "type": "turn/end",
                           "data": {"turn": 1,
                                    "reason": {"kind": "completed"}}}},
                ])
                value = {"accepted": True}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": body["rpcId"],
            "result": {"ok": True, "value": value},
        })

    native_client = httpx.AsyncClient(
        transport=httpx.MockTransport(dsh), base_url="http://dsh.test")
    adapter = DshWebSessionAdapter(
        client=native_client, poll_interval=0, timeout_seconds=1)
    app = build_app("dsh", agent_card, max_concurrent=1,
                    session_adapter=adapter)
    published: list[tuple] = []

    async def publish(*args, **kwargs):
        published.append((args, kwargs))
        return True

    app.state.publisher.publish = publish
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://adapter"
        ) as client:
            card = (await client.get("/.well-known/agent-card.json")).json()
            caps = card["capabilities"]["extensions"]["agentHubSession"]
            assert caps["native_resume"] is True
            assert caps["durable_session"] is True

            sent = (await client.post("/a2a", json={
                "jsonrpc": "2.0", "id": "send", "method": "message/send",
                "params": {"message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "review"}],
                    "metadata": {"taskId": "T-dsh-contract",
                                 "sessionId": "S-dsh-contract"},
                }},
            })).json()["result"]
            assert sent["id"] == "T-dsh-contract"

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                task = (await client.post("/a2a", json={
                    "jsonrpc": "2.0", "id": "poll", "method": "tasks/get",
                    "params": {"id": "T-dsh-contract"},
                })).json()["result"]
                if task["status"]["state"] == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError("DSH A2A task did not complete")
            meta = task["metadata"]["agentHub"]
            assert meta["nativeSessionId"] == "session-contract-dsh"
            assert task["artifacts"]
            session_events = [args for args, _ in published
                              if args[0] == "agent.session.event"]
            assert session_events
            assert all(event[1] == "T-dsh-contract"
                       for event in session_events)
            assert {event[2]["nativeEventType"] for event in session_events} >= {
                "dsh.turn.started", "dsh.assistant/message", "dsh.turn/end"}
    finally:
        await native_client.aclose()


async def test_dsh_a2a_native_approval_response_continues_same_turn(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "test-secret-0123456789abcdef")
    events: list[dict] = []
    response_rpc_ids: list[str] = []

    async def dsh(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/respond":
            response_rpc_ids.append(body["rpcId"])
            value = body["result"]["value"]
            events.extend([
                {"event": {"seq": 2, "type": "approval/decided",
                            "data": {"id": value["approvalId"],
                                     "outcome": value["outcome"]}}},
                {"event": {"seq": 3, "type": "turn/end",
                            "data": {"reason": {"kind": "completed"}}}},
            ])
            return httpx.Response(200, json={"accepted": True})
        method = body["method"]
        if method == "session.create":
            value = {"sessionId": "native-approval"}
        elif method == "session.history":
            value = {"events": events, "hasMore": False}
        elif method == "session.prompt":
            prompt = body["payload"]["content"][0]["text"]
            if not prompt.startswith("/permission "):
                events.extend([
                    {"event": {"seq": 0, "type": "turn/start",
                                "data": {"turn": 1}}},
                    {"event": {"seq": 1, "type": "approval/asked",
                                "data": {"id": "approval-1",
                                         "toolName": "bash",
                                         "reason": "modify"}}},
                ])
            value = {"accepted": True}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": body["rpcId"],
            "result": {"ok": True, "value": value},
        })

    native_client = httpx.AsyncClient(
        transport=httpx.MockTransport(dsh), base_url="http://dsh.test")
    adapter = DshWebSessionAdapter(
        client=native_client, poll_interval=0,
        timeout_seconds=2, interaction_wait_seconds=1)
    app = build_app("dsh", agent_card, max_concurrent=1,
                    session_adapter=adapter)

    async def publish(*args, **kwargs):
        return True

    app.state.publisher.publish = publish
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://adapter"
        ) as client:
            await client.post("/a2a", json={
                "jsonrpc": "2.0", "id": "send", "method": "message/send",
                "params": {"message": {
                    "role": "user", "parts": [{"kind": "text",
                                                  "text": "modify"}],
                    "metadata": {"taskId": "T-approval",
                                 "sessionId": "S-approval"},
                }},
            })
            deadline = time.monotonic() + 1
            while adapter.get_session("S-approval") is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("DSH session was not created")
                await asyncio.sleep(0.01)
            await adapter.ingest_server_request({
                "type": "server-request", "rpcId": "rpc-tool-1",
                "method": "session/event", "payload": {
                    "type": "session/event", "sessionId": "native-approval",
                    "event": {"type": "tool/call", "data": {
                        "callId": "call-1", "name": "bash",
                        "arguments": "hidden"}},
                    "view": {"for": "call", "view": {
                        "card": "terminal", "title": "touch safe.txt",
                        "cwd": str(tmp_path)}},
                },
            })
            await adapter.ingest_server_request({
                "type": "server-request", "rpcId": "rpc-approval-1",
                "method": "approval/requested", "payload": {
                    "type": "approval/requested",
                    "sessionId": "native-approval",
                    "approvalId": "approval-1", "toolName": "bash",
                    "callId": "call-1",
                    "reason": "modify",
                },
            })

            deadline = time.monotonic() + 2
            while True:
                task = (await client.post("/a2a", json={
                    "jsonrpc": "2.0", "id": "poll", "method": "tasks/get",
                    "params": {"id": "T-approval"},
                })).json()["result"]
                if task["status"]["state"] == "input-required":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("approval was not surfaced")
                await asyncio.sleep(0.01)
            pending = task["metadata"]["agentHub"]["pendingInteractions"]
            assert pending[0]["nativeRequestId"] == "rpc-approval-1"
            assert pending[0]["payload"]["toolView"]["command"] == \
                "touch safe.txt"

            from common.action_receipt import sign_action_receipt

            authorization = sign_action_receipt({
                "actionIntentId": "AI-approved", "status": "approved",
                "decidedBy": "hermes", "decidedAt": "now",
                "basedOnRevision": 1, "taskId": "T-approval",
                "interactionId": pending[0]["interactionId"],
                "nativeRequestId": "rpc-approval-1",
            })
            responded = (await client.post("/a2a", json={
                "jsonrpc": "2.0", "id": "respond",
                "method": "extensions/session/interactions/respond",
                "params": {
                    "id": "T-approval",
                    "interactionId": pending[0]["interactionId"],
                    "respondedBy": "hermes",
                    "response": {
                        "outcome": "allowed-once",
                        "authorization": authorization,
                    },
                },
            })).json()
            assert responded["result"]["status"]["state"] == "working"
            deadline = time.monotonic() + 2
            while True:
                task = (await client.post("/a2a", json={
                    "jsonrpc": "2.0", "id": "done", "method": "tasks/get",
                    "params": {"id": "T-approval"},
                })).json()["result"]
                if task["status"]["state"] == "completed":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("same DSH turn did not complete")
                await asyncio.sleep(0.01)
            assert response_rpc_ids == ["rpc-approval-1"]
    finally:
        await native_client.aclose()
