"""Adapter shutdown must terminalize in-flight background execution."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from adapters.server_common import build_app
from adapters.session import (
    SessionAdapter, SessionCapabilities, SessionHandle, SessionTurnResult,
)

pytestmark = pytest.mark.anyio


def _card(base_url: str) -> dict:
    return {"name": "slow", "url": base_url, "skills": []}


async def test_lifespan_shutdown_fails_active_background_task():
    entered = asyncio.Event()

    async def runner(task):
        entered.set()
        await asyncio.Event().wait()

    app = build_app("slow", _card, runner)
    published = []

    async def publish(*args, **kwargs):
        published.append((args, kwargs))
        return True

    app.state.publisher.publish = publish
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://adapter",
    ) as client:
        response = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "send", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "wait"}],
                "metadata": {"taskId": "T-shutdown"},
            }},
        })
        assert response.json()["result"]["id"] == "T-shutdown"
        await asyncio.wait_for(entered.wait(), timeout=1)

    task = app.state.store.get("T-shutdown")
    assert task.status_state == "failed"
    failure = next(
        args for args, _ in published if args[0] == "task.failed")
    assert failure[2]["reason"] == "adapter_shutdown"
    assert failure[2]["status_to"] == "failed"


async def test_shutdown_fails_task_if_terminal_event_was_not_delivered():
    terminal_publish_started = asyncio.Event()

    async def runner(task):
        return []

    app = build_app("terminal-window", _card, runner)
    published = []

    async def publish(*args, **kwargs):
        if args[0] == "task.completed":
            terminal_publish_started.set()
            await asyncio.Event().wait()
        published.append((args, kwargs))
        return True

    app.state.publisher.publish = publish
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://adapter",
    ) as client:
        response = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "send", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "finish"}],
                "metadata": {"taskId": "T-terminal-window"},
            }},
        })
        assert response.json()["result"]["id"] == "T-terminal-window"
        await asyncio.wait_for(terminal_publish_started.wait(), timeout=1)

    task = app.state.store.get("T-terminal-window")
    assert task.status_state == "failed"
    failure = next(
        args for args, _ in published if args[0] == "task.failed")
    assert failure[2]["reason"] == "adapter_shutdown"
    assert failure[2]["status_from"] == "completed"


async def test_shutdown_fails_replaced_task_after_prior_generation_completed():
    first_completed = asyncio.Event()
    second_entered = asyncio.Event()
    calls = 0

    async def runner(task):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        second_entered.set()
        await asyncio.Event().wait()
        return []

    app = build_app("replacement", _card, runner)
    published = []

    async def publish(*args, **kwargs):
        published.append((args, kwargs))
        if args[0] == "task.completed":
            first_completed.set()
        return True

    app.state.publisher.publish = publish
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://adapter",
    ) as client:
        first = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "first", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "finish first"}],
                "metadata": {"taskId": "T-replacement"},
            }},
        })
        assert first.json()["result"]["id"] == "T-replacement"
        await asyncio.wait_for(first_completed.wait(), timeout=1)

        replacement = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "replacement", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "wait second"}],
                "metadata": {
                    "taskId": "T-replacement", "replaceSession": True,
                },
            }},
        })
        assert replacement.json()["result"]["id"] == "T-replacement"
        await asyncio.wait_for(second_entered.wait(), timeout=1)

    task = app.state.store.get("T-replacement")
    assert task.status_state == "failed"
    failures = [
        args for args, _ in published
        if args[0] == "task.failed" and args[1] == "T-replacement"
    ]
    assert len(failures) == 1
    assert failures[0][2]["reason"] == "adapter_shutdown"


async def test_shutdown_old_terminal_publish_cannot_fail_replacement_task():
    first_terminal_started = asyncio.Event()
    calls = 0

    async def runner(task):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        await asyncio.Event().wait()

    app = build_app("replacement-window", _card, runner)
    published = []

    async def publish(*args, **kwargs):
        if args[0] == "task.completed":
            first_terminal_started.set()
            await asyncio.Event().wait()
        published.append((args, kwargs))
        return True

    app.state.publisher.publish = publish
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://adapter",
    ) as client:
        first = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "first", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "finish first"}],
                "metadata": {"taskId": "T-replacement-window"},
            }},
        })
        assert first.json()["result"]["id"] == "T-replacement-window"
        await asyncio.wait_for(first_terminal_started.wait(), timeout=1)
        replacement = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "replacement",
            "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "replacement"}],
                "metadata": {
                    "taskId": "T-replacement-window", "replaceSession": True,
                },
            }},
        })
        assert replacement.json()["result"]["id"] == \
            "T-replacement-window"

    task = app.state.store.get("T-replacement-window")
    assert task.status_state == "failed"
    failures = [args for args, _ in published if args[0] == "task.failed"]
    assert len(failures) == 1
    assert failures[0][2]["status_from"] == "submitted"
    discarded = [args for args, _ in published
                 if args[0] == "session.result_discarded"]
    assert discarded[0][2]["reason"] == "execution_generation_replaced"


async def test_shutdown_spools_failure_when_nats_is_unavailable(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("LAS_NATS_URL", "nats://127.0.0.1:1")
    entered = asyncio.Event()

    async def runner(task):
        entered.set()
        await asyncio.Event().wait()

    app = build_app("offline", _card, runner)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://adapter",
    ) as client:
        response = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "send", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "wait offline"}],
                "metadata": {"taskId": "T-offline-shutdown"},
            }},
        })
        assert response.json()["result"]["id"] == "T-offline-shutdown"
        await asyncio.wait_for(entered.wait(), timeout=3)

    spool = tmp_path / "workspace" / "logs" / "events-pending.jsonl"
    events = [json.loads(line) for line in spool.read_text().splitlines()]
    failure = next(
        event for event in events
        if event["event_type"] == "task.failed"
        and event["task_id"] == "T-offline-shutdown")
    assert failure["payload"]["reason"] == "adapter_shutdown"


async def test_failed_task_discards_turn_already_waiting_in_fifo():
    class QueuedSessionAdapter(SessionAdapter):
        capabilities = SessionCapabilities(multi_turn=True)

        def __init__(self):
            self.handle = None
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.messages = 0

        async def start_session(self, task, *, session_id, metadata):
            self.handle = SessionHandle(session_id=session_id, task_id=task.id)
            return self.handle

        async def send_message(self, session_id, message):
            self.messages += 1
            if self.messages == 1:
                self.entered.set()
                await self.release.wait()
            return SessionTurnResult(state="completed")

        def get_session(self, session_id):
            return self.handle

    adapter = QueuedSessionAdapter()
    app = build_app("queued", _card, session_adapter=adapter)
    published = []

    async def publish(*args, **kwargs):
        published.append((args, kwargs))
        return True

    app.state.publisher.publish = publish
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://adapter",
    ) as client:
        body = {
            "jsonrpc": "2.0", "method": "message/send",
            "params": {"message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "turn"}],
                "metadata": {"taskId": "T-queued-failure"},
            }},
        }
        first = await client.post("/a2a", json={**body, "id": "first"})
        assert first.json()["result"]["id"] == "T-queued-failure"
        await asyncio.wait_for(adapter.entered.wait(), timeout=1)
        second = await client.post("/a2a", json={**body, "id": "second"})
        assert second.json()["result"]["id"] == "T-queued-failure"

        app.state.store.update_state(
            "T-queued-failure", "failed", error="native runtime unavailable")
        adapter.release.set()
        await asyncio.sleep(0.05)

        assert adapter.messages == 1
        assert app.state.store.get("T-queued-failure").status_state == "failed"
        assert len([args for args, _ in published
                    if args[0] == "task.started"]) == 1
