"""Adapter readiness must gate online discovery heartbeats."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from adapters.server_common import _heartbeat_loop
from adapters.session import SessionEvent

pytestmark = pytest.mark.anyio


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def publish(self, *args) -> bool:
        self.calls.append(args)
        return True

    async def replay_pending(self) -> int:
        return 0


def _card(_: str) -> dict:
    return {"skills": [{"id": "review"}]}


async def _run_first_heartbeat(
    publisher: Publisher, health_check, adapter_instance_id=None,
    adapter_started_at=None, session_adapter=None,
) -> None:
    task = asyncio.create_task(_heartbeat_loop(
        publisher, "worker", _card, health_check, adapter_instance_id,
        adapter_started_at, session_adapter))
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

    await _run_first_heartbeat(
        publisher, health, "worker-instance-1", "2026-08-29T12:00:00+00:00")
    assert publisher.calls[0][0] == "agent.worker.heartbeat"
    assert publisher.calls[0][2]["skills"] == ["review"]
    assert publisher.calls[0][2]["adapterInstanceId"] == "worker-instance-1"
    assert publisher.calls[0][2]["adapterStartedAt"] == \
        "2026-08-29T12:00:00+00:00"


async def test_heartbeat_publishes_terminal_recovery_event():
    publisher = Publisher()
    event = SessionEvent(
        event_type="task.failed", session_id="S-recovery",
        task_id="T-recovery",
        payload={
            "status_from": "input-required",
            "status_to": "failed",
            "error": "native runtime unavailable",
            "reason": "native_runtime_unavailable",
            "interaction_ids": ["I-recovery"],
        },
    )

    class RecoveryAdapter:
        def __init__(self):
            self.events = [event]

        def drain_recovery_events(self):
            events, self.events = self.events, []
            return events

    async def health() -> dict:
        return {"ready": True}

    await _run_first_heartbeat(
        publisher, health, "worker-instance-1", session_adapter=RecoveryAdapter())

    recovery = next(call for call in publisher.calls
                    if call[0] == "task.failed")
    assert recovery[1] == "T-recovery"
    assert recovery[2]["session_id"] == "S-recovery"
    assert recovery[2]["reason"] == "native_runtime_unavailable"
    assert recovery[2]["interaction_ids"] == ["I-recovery"]
