"""AgentHub Session extension contract, exercised by the Fake Adapter."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.anyio


async def _rpc(client, method, params, rpc_id):
    response = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params,
    })
    assert response.status_code == 200
    return response.json()


async def _wait_state(client, task_id, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await _rpc(client, "tasks/get", {"id": task_id}, "poll")
        if result["result"]["status"]["state"] == expected:
            return result["result"]
        await asyncio.sleep(0.01)
    raise TimeoutError(f"task {task_id} did not reach {expected}")


async def test_same_task_accepts_ordered_turns_and_session_controls(
        tmp_path, monkeypatch):
    import adapters.fake.session as fake_module
    from adapters.fake.server import create_app

    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(fake_module, "FAKE_LATENCY_SECONDS", 0)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5
    ) as client:
        card = (await client.get("/.well-known/agent-card.json")).json()
        caps = card["capabilities"]["extensions"]["agentHubSession"]
        assert caps["multi_turn"] is True
        assert caps["native_resume"] is False

        first = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "plan"}],
            "metadata": {
                "taskId": "T-session", "sessionId": "S-session",
                "contextRevision": 4, "interactive": True,
                "idempotencyKey": "turn-1",
            },
        }}, "turn-1")
        assert first["result"]["id"] == "T-session"
        await _wait_state(client, "T-session", "input-required")

        paused = await _rpc(
            client, "extensions/session/pause", {"id": "T-session"}, "pause")
        assert paused["result"]["status"]["state"] == "paused"
        resumed = await _rpc(
            client, "extensions/session/resume", {"id": "T-session"}, "resume")
        assert resumed["result"]["status"]["state"] == "input-required"
        interrupted = await _rpc(
            client, "extensions/session/interrupt", {"id": "T-session"},
            "interrupt")
        assert interrupted["result"]["status"]["state"] == "paused"
        await _rpc(
            client, "extensions/session/resume", {"id": "T-session"},
            "resume-2")

        second = await _rpc(client, "message/send", {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "implement corrected plan"}],
            "metadata": {
                "taskId": "T-session", "contextRevision": 5,
                "completeSession": True, "idempotencyKey": "turn-2",
            },
        }}, "turn-2")
        assert second["result"]["metadata"]["agentHub"]["sessionId"] == \
            "S-session"
        completed = await _wait_state(client, "T-session", "completed")
        assert completed["metadata"]["agentHub"]["contextRevision"] == 5
        assert len(completed["history"]) == 2
        assert completed["artifacts"]

        stale = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "stale"}],
            "metadata": {"taskId": "T-session", "contextRevision": 4},
        }}, "stale")
        # Terminal-state rejection has priority here; stale revision is covered
        # separately on a live task below.
        assert stale["error"]["code"] == -32003


async def test_stale_context_revision_is_rejected(tmp_path, monkeypatch):
    import adapters.fake.session as fake_module
    from adapters.fake.server import create_app

    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(fake_module, "FAKE_LATENCY_SECONDS", 0)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5
    ) as client:
        await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "plan"}],
            "metadata": {
                "taskId": "T-revision", "contextRevision": 8,
                "interactive": True,
            },
        }}, "start")
        await _wait_state(client, "T-revision", "input-required")
        stale = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "old"}],
            "metadata": {
                "taskId": "T-revision", "contextRevision": 7,
                "idempotencyKey": "revision-retry",
            },
        }}, "stale")
        assert stale["error"]["code"] == -32005
        corrected = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "new"}],
            "metadata": {
                "taskId": "T-revision", "contextRevision": 9,
                "idempotencyKey": "revision-retry", "interactive": True,
            },
        }}, "corrected")
        assert corrected["result"]["id"] == "T-revision"
        current = await _wait_state(client, "T-revision", "input-required")
        assert len(current["history"]) == 2


async def test_cancel_is_terminal(tmp_path, monkeypatch):
    import adapters.fake.session as fake_module
    from adapters.fake.server import create_app

    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(fake_module, "FAKE_LATENCY_SECONDS", 0.05)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5
    ) as client:
        await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "wait"}],
            "metadata": {"taskId": "T-cancel", "interactive": True},
        }}, "start")
        await _wait_state(client, "T-cancel", "input-required")
        canceled = await _rpc(
            client, "tasks/cancel", {"id": "T-cancel"}, "cancel")
        assert canceled["result"]["status"]["state"] == "canceled"
        rejected = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "more"}],
            "metadata": {"taskId": "T-cancel"},
        }}, "after-cancel")
        assert rejected["error"]["code"] == -32003


async def test_terminal_task_requires_explicit_recovery_replacement(
        tmp_path, monkeypatch):
    import adapters.fake.session as fake_module
    from adapters.fake.server import create_app

    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(fake_module, "FAKE_LATENCY_SECONDS", 0)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=5
    ) as client:
        await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "first"}],
            "metadata": {
                "taskId": "T-replace", "sessionId": "S-old",
                "idempotencyKey": "replace-1",
            },
        }}, "first")
        await _wait_state(client, "T-replace", "completed")
        replayed = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "first"}],
            "metadata": {
                "taskId": "T-replace", "sessionId": "S-old",
                "idempotencyKey": "replace-1",
            },
        }}, "replay")
        assert replayed["result"]["status"]["state"] == "completed"
        assert len(replayed["result"]["history"]) == 1

        conflict = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "other"}],
            "metadata": {
                "taskId": "T-other", "idempotencyKey": "replace-1",
            },
        }}, "conflict")
        assert conflict["error"]["code"] == -32004

        denied = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "retry"}],
            "metadata": {"taskId": "T-replace", "sessionId": "S-new"},
        }}, "denied")
        assert denied["error"]["code"] == -32003

        replaced = await _rpc(client, "message/send", {"message": {
            "role": "user", "parts": [{"kind": "text", "text": "retry"}],
            "metadata": {
                "taskId": "T-replace", "sessionId": "S-new",
                "replaceSession": True, "recoveryMode": "replacement",
                "idempotencyKey": "replace-2",
            },
        }}, "replacement")
        assert replaced["result"]["metadata"]["agentHub"]["sessionId"] == \
            "S-new"
        final = await _wait_state(client, "T-replace", "completed", timeout=10)
        assert final["metadata"]["agentHub"]["sessionId"] == "S-new"
