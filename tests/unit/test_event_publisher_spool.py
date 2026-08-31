"""Cross-process spool locking must not archive newly appended events."""

from __future__ import annotations

import asyncio
import json

import pytest

from adapters.common import EventPublisher
from common.events import Event

pytestmark = pytest.mark.anyio


async def test_append_waits_for_replay_rotation_without_losing_event(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    first = EventPublisher("codex", "nats://unused")
    second = EventPublisher("dsh", "nats://unused")
    old_event = Event(
        event_type="task.failed", source="codex", task_id="T-old",
        payload={"reason": "offline"})
    new_event = Event(
        event_type="task.failed", source="dsh", task_id="T-new",
        payload={"reason": "offline"})
    await first._spool(old_event)

    replay_read = asyncio.Event()
    allow_rotate = asyncio.Event()

    async def replay_spool(path, nats_url):
        lines = path.read_text(encoding="utf-8").splitlines()
        replay_read.set()
        await allow_rotate.wait()
        path.rename(path.with_suffix(".replayed-test.jsonl"))
        return len(lines)

    monkeypatch.setattr(
        "orchestrator.nats_client.replay_spool", replay_spool)
    replay = asyncio.create_task(first.replay_pending())
    await asyncio.wait_for(replay_read.wait(), timeout=1)
    append = asyncio.create_task(second._spool(new_event))
    await asyncio.sleep(0.03)
    assert not append.done()

    allow_rotate.set()
    assert await asyncio.wait_for(replay, timeout=1) == 1
    await asyncio.wait_for(append, timeout=1)

    archived = first.spool.with_suffix(".replayed-test.jsonl")
    assert json.loads(archived.read_text().splitlines()[0])["task_id"] == \
        "T-old"
    current = [json.loads(line) for line in first.spool.read_text().splitlines()]
    assert [event["task_id"] for event in current] == ["T-new"]
