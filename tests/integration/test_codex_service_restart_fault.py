"""Real Codex HTTP Adapter process restart and native thread recovery drill."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from orchestrator.a2a_client import A2aClient

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_CODEX_SERVICE_RESTART") != "1",
        reason="set LAS_RUN_CODEX_SERVICE_RESTART=1 for HTTP restart drill",
    ),
    pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed"),
]

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_adapter(port: int, workspace: Path, log: Path) -> subprocess.Popen:
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src"),
        AGENT_WORKSPACE=str(workspace),
        LAS_ADAPTER_BIND="127.0.0.1",
        LAS_ADAPTER_PORT=str(port),
        LAS_ADAPTER_TOKEN="",
        LAS_ACTION_RECEIPT_SECRET="r" * 32,
        NATS_URL="nats://127.0.0.1:1",
    )
    with log.open("ab") as output:
        return subprocess.Popen(
            [str(ROOT / ".venv/bin/python"),
             str(ROOT / "scripts/serve_adapter.py"), "codex"],
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


async def _wait_ready(base_url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise AssertionError(
                f"Codex Adapter exited with {proc.returncode}: {detail}")
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(f"{base_url}/health")
            if response.status_code == 200:
                return
        except httpx.TransportError as exc:
            last_error = exc
        await asyncio.sleep(0.1)
    raise AssertionError(f"Codex Adapter not ready: {last_error}")


def _summary(task: dict) -> str:
    artifact = next(
        item for item in task["artifacts"] if item["name"] == "last-message.md")
    return Path(artifact["path"]).read_text(encoding="utf-8")


async def test_http_adapter_restart_resumes_same_codex_thread(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "logs").mkdir(parents=True)
    log = tmp_path / "codex-adapter.log"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    client = A2aClient(base_url, timeout=180)
    proc: subprocess.Popen | None = None
    try:
        proc = _start_adapter(port, workspace, log)
        await _wait_ready(base_url, proc, log)
        first = await client.send_and_wait(
            "Do not call tools. Reply exactly CODEX_HTTP_RESTART_MARKER.",
            idempotency_key="codex-http-restart:1",
            task_id="T-CODEX-HTTP-RESTART",
            timeout=180,
        )
        assert first["status"]["state"] == "completed"
        assert "CODEX_HTTP_RESTART_MARKER" in _summary(first)
        native = first["metadata"]["agentHub"]["nativeSessionId"]
        assert native

        _stop(proc)
        proc = _start_adapter(port, workspace, log)
        await _wait_ready(base_url, proc, log)
        second = await client.send_message(
            "Do not call tools. Reply CODEX_HTTP_RESUME_OK: followed by the "
            "exact marker from my previous turn.",
            idempotency_key="codex-http-restart:2",
            task_id="T-CODEX-HTTP-RESTART",
            native_session_id=native,
            context_revision=2,
            replace_session=True,
        )
        terminal = {"completed", "failed", "canceled", "rejected"}
        deadline = time.monotonic() + 180
        while second["status"]["state"] not in terminal:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.25)
            second = await client.get_task(second["id"])

        assert second["status"]["state"] == "completed"
        assert second["metadata"]["agentHub"]["nativeSessionId"] == native
        summary = _summary(second)
        assert "CODEX_HTTP_RESUME_OK:" in summary
        assert "CODEX_HTTP_RESTART_MARKER" in summary
    finally:
        _stop(proc)
