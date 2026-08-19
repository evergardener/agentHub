"""A2A same-turn steer contract for capable native adapters."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from adapters.server_common import build_app
from adapters.session import (
    SessionAdapter, SessionCapabilities, SessionHandle, SessionMessage,
    SessionTurnResult,
)

pytestmark = pytest.mark.anyio


class SteerAdapter(SessionAdapter):
    capabilities = SessionCapabilities(
        multi_turn=True, resume=True, native_resume=True,
        durable_session=True, steer=True)

    def __init__(self):
        self.handle = None
        self.release = asyncio.Event()
        self.steers: list[SessionMessage] = []

    async def start_session(self, task, *, session_id, metadata):
        self.handle = SessionHandle(
            session_id=session_id, task_id=task.id,
            native_session_id="native-steer")
        return self.handle

    def get_session(self, session_id):
        return self.handle

    async def send_message(self, session_id, message):
        await self.release.wait()
        return SessionTurnResult(state="completed")

    async def steer(self, session_id, message):
        self.steers.append(message)
        self.handle.context_revision = message.based_on_revision
        self.release.set()
        return self.handle


def card(base_url):
    return {
        "name": "steer", "url": base_url, "version": "0.1.0",
        "capabilities": {}, "skills": [],
    }


async def test_a2a_steer_reaches_current_turn_with_revision():
    adapter = SteerAdapter()
    app = build_app("steer", card, session_adapter=adapter)

    async def publish(*args, **kwargs):
        return True

    app.state.publisher.publish = publish
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://adapter"
    ) as client:
        await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "send", "method": "message/send",
            "params": {"message": {
                "role": "user", "parts": [{"kind": "text", "text": "build"}],
                "metadata": {"taskId": "T-steer", "sessionId": "S-steer"},
            }},
        })
        deadline = time.monotonic() + 1
        while adapter.handle is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("session did not start")
            await asyncio.sleep(0.01)
        steer_payload = {
            "jsonrpc": "2.0", "id": "steer",
            "method": "extensions/session/steer",
            "params": {
                "id": "T-steer",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "only change API"}],
                    "metadata": {"messageId": "M-steer", "contextRevision": 2},
                },
            },
        }
        response = (await client.post("/a2a", json=steer_payload)).json()
        assert response["result"]["status"]["state"] == "working"
        assert adapter.steers[0].content == "only change API"
        assert adapter.steers[0].based_on_revision == 2
        duplicate = (await client.post("/a2a", json=steer_payload)).json()
        assert "result" in duplicate
        assert len(adapter.steers) == 1
