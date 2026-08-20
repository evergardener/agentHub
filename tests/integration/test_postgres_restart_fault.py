"""Isolated PostgreSQL restart fault test for the durable State Writer.

This test creates a unique Docker Compose project with temporary volumes and
host ports.  It never targets the default AgentHub Compose project.  Running it
requires an explicit ``LAS_RUN_PG_FAULTS=1`` opt-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import nats
import pytest

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.nats_client import durable_consume, ensure_stream
from state.writer import StateWriter

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_PG_FAULTS") != "1",
        reason="set LAS_RUN_PG_FAULTS=1 for isolated PostgreSQL restart test",
    ),
]

POSTGRES_IMAGE = (
    "postgres:17.11-alpine3.24@"
    "sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
)
NATS_IMAGE = (
    "nats:2.11.17-alpine3.22@"
    "sha256:e4bf19f15fd3218814a4e3c9e0064e1334bd8aa20d5984b9f1a0afd084f8cc00"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _compose(compose: Path, project: str, *args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "-p", project, *args],
        check=True,
        text=True,
        stdout=subprocess.DEVNULL,
    )


def _cleanup_compose(compose: Path, project: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(compose), "-p", project,
         "down", "-v", "--remove-orphans"],
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _publish(nats_url: str, event: dict) -> None:
    nc = await nats.connect(
        nats_url, connect_timeout=2,
        max_reconnect_attempts=1, allow_reconnect=False,
    )
    try:
        await nc.jetstream().publish(
            event["event_type"], json.dumps(event).encode("utf-8"))
    finally:
        await nc.close()


async def _wait_until(predicate, message: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(message)


async def _wait_nats(nats_url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            await ensure_stream(nats_url)
            return
        except Exception:
            await asyncio.sleep(0.2)
    raise AssertionError("isolated NATS did not become ready")


async def test_state_writer_recovers_after_postgres_restart_once(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")

    pg_port = _free_port()
    nats_port = _free_port()
    project = f"agenthub_pg_fault_{uuid.uuid4().hex[:10]}"
    compose = tmp_path / "compose.yml"
    compose.write_text(textwrap.dedent(f"""
        services:
          postgres:
            image: {POSTGRES_IMAGE}
            environment:
              POSTGRES_USER: agenthub
              POSTGRES_PASSWORD: fault-only-password
              POSTGRES_DB: agenthub
            ports: ["127.0.0.1:{pg_port}:5432"]
            volumes: [pg-data:/var/lib/postgresql/data]
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U agenthub -d agenthub"]
              interval: 1s
              timeout: 2s
              retries: 30
          nats:
            image: {NATS_IMAGE}
            command: ["-js", "-sd", "/data"]
            ports: ["127.0.0.1:{nats_port}:4222"]
            volumes: [nats-data:/data]
        volumes:
          pg-data:
          nats-data:
    """), encoding="utf-8")
    pg_url = (
        f"postgresql://agenthub:fault-only-password@127.0.0.1:{pg_port}/agenthub"
        "?connect_timeout=1"
    )
    nats_url = f"nats://127.0.0.1:{nats_port}"
    writer: StateWriter | None = None
    consumer: asyncio.Task | None = None
    stop = asyncio.Event()
    attempts = 0
    failures = 0

    try:
        _compose(compose, project, "up", "-d", "--wait")
        await _wait_nats(nats_url)
        writer = StateWriter(pg_url)
        task_id = "T-PG-RESTART-1"
        state_store.create_task(
            writer.conn, task_id=task_id, objective="postgres restart delivery",
            created_by="hermes", status=TaskStatus.QUEUED,
        )
        state_store.transition_task(writer.conn, task_id, TaskStatus.ASSIGNED)

        async def apply(event: dict) -> None:
            nonlocal attempts, failures
            attempts += 1
            try:
                assert writer is not None
                writer.apply_resilient(event)
            except Exception:
                failures += 1
                raise

        consumer = asyncio.create_task(durable_consume(
            f"pg-fault-{uuid.uuid4().hex[:8]}", apply, nats_url,
            stop_event=stop,
        ))
        await _publish(nats_url, {
            "event_id": "E-PG-STARTED", "event_type": "task.started",
            "task_id": task_id, "source": "codex",
            "payload": {"attempt": 1},
        })
        await _wait_until(
            lambda: state_store.get_task(writer.conn, task_id)["status"]
            == "working",
            "initial event did not reach PostgreSQL",
        )

        _compose(compose, project, "stop", "postgres")
        completed = {
            "event_id": "E-PG-COMPLETED", "event_type": "task.completed",
            "task_id": task_id, "source": "codex",
            "payload": {"attempt": 1, "summary": "recovered"},
        }
        await _publish(nats_url, completed)
        await _publish(nats_url, completed)
        await _wait_until(
            lambda: failures >= 1,
            "database outage did not NAK the durable message",
        )
        assert consumer is not None and not consumer.done()

        _compose(compose, project, "up", "-d", "--wait", "postgres")
        await _wait_until(
            lambda: state_store.get_task(writer.conn, task_id)["status"]
            == "completed",
            "State Writer did not reconnect and apply the redelivery",
            timeout=30,
        )
        assert attempts >= 3  # started + failed delivery + recovered delivery
        assert writer.conn.execute(
            "SELECT COUNT(*) FROM events WHERE id = ?;",
            ("E-PG-COMPLETED",),
        ).fetchone()[0] == 1
        assert writer.conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?"
            " AND status = 'completed';",
            (task_id,),
        ).fetchone()[0] == 1
    finally:
        stop.set()
        if consumer is not None:
            try:
                await asyncio.wait_for(consumer, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                consumer.cancel()
        if writer is not None:
            try:
                writer.conn.close()
            except Exception:
                pass
        _cleanup_compose(compose, project)
