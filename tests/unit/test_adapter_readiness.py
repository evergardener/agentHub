"""Adapter readiness must gate online discovery heartbeats."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from adapters.server_common import _heartbeat_loop

pytestmark = pytest.mark.anyio


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def publish(self, *args) -> bool:
        self.calls.append(args)
        return True


def _card(_: str) -> dict:
    return {"skills": [{"id": "review"}]}


async def _run_first_heartbeat(publisher: Publisher, health_check) -> None:
    task = asyncio.create_task(_heartbeat_loop(
        publisher, "worker", _card, health_check))
    try:
        await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_unready_dependency_does_not_publish_online_heartbeat():
    publisher = Publisher()

    async def health() -> dict:
        return {"ready": False}

    await _run_first_heartbeat(publisher, health)
    assert publisher.calls == []


async def test_ready_dependency_publishes_online_heartbeat():
    publisher = Publisher()

    async def health() -> dict:
        return {"ready": True}

    await _run_first_heartbeat(publisher, health)
    assert publisher.calls[0][0] == "agent.worker.heartbeat"
    assert publisher.calls[0][2]["skills"] == ["review"]
