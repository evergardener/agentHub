"""Real DSH Web + HTTP Adapter restart recovery with a two-turn model task."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from adapters.dsh.session import DshWebSessionAdapter
from orchestrator.a2a_client import A2aClient

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_DSH_SERVICE_RESTART") != "1",
        reason="set LAS_RUN_DSH_SERVICE_RESTART=1 for real DSH restart drill",
    ),
    pytest.mark.skipif(not shutil.which("dsh"), reason="dsh CLI not installed"),
]

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_dsh(port: int, log: Path) -> subprocess.Popen:
    dsh = shutil.which("dsh")
    if dsh is None:
        raise RuntimeError("dsh CLI not installed")
    with log.open("ab") as output:
        return subprocess.Popen(
            [dsh, "web", "--host", "127.0.0.1", "--port", str(port)],
            env=dict(os.environ),
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def _start_adapter(
    port: int, dsh_url: str, workspace: Path, log: Path
) -> subprocess.Popen:
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src"),
        AGENT_WORKSPACE=str(workspace),
        LAS_ADAPTER_BIND="127.0.0.1",
        LAS_ADAPTER_PORT=str(port),
        LAS_ADAPTER_TOKEN="",
        LAS_DSH_WEB_URL=dsh_url,
        LAS_DSH_PERMISSION_PRESET="read-only",
        LAS_DSH_AGENT_PRESET="standard",
        LAS_DSH_ALLOW_UNVERIFIED_RUNTIME="false",
        LAS_PRODUCTION_MODE="false",
        LAS_ACTION_RECEIPT_SECRET="d" * 32,
        NATS_URL="nats://127.0.0.1:1",
    )
    with log.open("ab") as output:
        return subprocess.Popen(
            [str(ROOT / ".venv/bin/python"),
             str(ROOT / "scripts/serve_adapter.py"), "dsh"],
            cwd=ROOT,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def _wait_dsh(url: str, proc: subprocess.Popen, log: Path) -> None:
    adapter = DshWebSessionAdapter(
        base_url=url, event_stream=False, timeout_seconds=5)
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = log.read_text(
                    encoding="utf-8", errors="replace")[-4000:]
                raise AssertionError(
                    f"DSH Web exited with {proc.returncode}: {detail}")
            try:
                await adapter._request("session.list", {})
                return
            except Exception as exc:  # startup boundary
                last_error = exc
                await asyncio.sleep(0.2)
    finally:
        await adapter.close()
    raise AssertionError(f"DSH Web not ready: {last_error}")


async def _wait_adapter(
    base_url: str, proc: subprocess.Popen, log: Path
) -> None:
    client = A2aClient(base_url, timeout=2)
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise AssertionError(
                f"DSH Adapter exited with {proc.returncode}: {detail}")
        try:
            await client.health()
            return
        except Exception as exc:  # startup boundary
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(f"DSH Adapter not ready: {last_error}")


def _summary(task: dict) -> str:
    artifact = next(
        item for item in task["artifacts"] if item["name"] == "last-message.md")
    return Path(artifact["path"]).read_text(encoding="utf-8")


async def _wait_terminal(client: A2aClient, task: dict) -> dict:
    terminal = {"completed", "failed", "canceled", "rejected"}
    deadline = time.monotonic() + 180
    while task["status"]["state"] not in terminal:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.25)
        task = await client.get_task(task["id"])
    return task


async def test_dsh_web_and_http_adapter_restart_resume_same_session(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "logs").mkdir(parents=True)
    dsh_log = tmp_path / "dsh.log"
    adapter_log = tmp_path / "adapter.log"
    dsh_port, adapter_port = _free_port(), _free_port()
    while adapter_port == dsh_port:
        adapter_port = _free_port()
    dsh_url = f"http://127.0.0.1:{dsh_port}"
    adapter_url = f"http://127.0.0.1:{adapter_port}"
    client = A2aClient(adapter_url, timeout=180)
    dsh_proc: subprocess.Popen | None = None
    adapter_proc: subprocess.Popen | None = None
    try:
        dsh_proc = _start_dsh(dsh_port, dsh_log)
        await _wait_dsh(dsh_url, dsh_proc, dsh_log)
        adapter_proc = _start_adapter(
            adapter_port, dsh_url, workspace, adapter_log)
        await _wait_adapter(adapter_url, adapter_proc, adapter_log)
        first = await client.send_and_wait(
            "Do not call tools. Reply exactly DSH_RESTART_MARKER.",
            idempotency_key="dsh-service-restart:1",
            task_id="T-DSH-SERVICE-RESTART",
            timeout=180,
        )
        assert first["status"]["state"] == "completed"
        assert "DSH_RESTART_MARKER" in _summary(first)
        native = first["metadata"]["agentHub"]["nativeSessionId"]
        assert native

        _stop(adapter_proc)
        _stop(dsh_proc)
        dsh_proc = _start_dsh(dsh_port, dsh_log)
        await _wait_dsh(dsh_url, dsh_proc, dsh_log)
        adapter_proc = _start_adapter(
            adapter_port, dsh_url, workspace, adapter_log)
        await _wait_adapter(adapter_url, adapter_proc, adapter_log)
        second = await client.send_message(
            "Do not call tools. Reply DSH_RESUME_OK: followed by the exact "
            "marker from my previous turn.",
            idempotency_key="dsh-service-restart:2",
            task_id="T-DSH-SERVICE-RESTART",
            native_session_id=native,
            context_revision=2,
            replace_session=True,
        )
        second = await _wait_terminal(client, second)
        assert second["status"]["state"] == "completed"
        assert second["metadata"]["agentHub"]["nativeSessionId"] == native
        summary = _summary(second)
        assert "DSH_RESUME_OK:" in summary
        assert "DSH_RESTART_MARKER" in summary
    finally:
        _stop(adapter_proc)
        _stop(dsh_proc)
