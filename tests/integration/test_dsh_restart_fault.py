"""Opt-in DSH Web + Adapter restart drill without any model request.

The drill uses a temporary ``DSH_HOME`` and random loopback port. It only calls
session.create/list/history, so it neither mutates the user's ~/.dsh profile nor
submits an LLM prompt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from adapters.common import A2aTask
from adapters.dsh.session import DshWebSessionAdapter

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_DSH_RESTART") != "1",
        reason="set LAS_RUN_DSH_RESTART=1 for isolated DSH restart drill",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_dsh(dsh: str, dsh_home: Path, port: int, log: Path):
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)
    dsh_home.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        return subprocess.Popen(
            [dsh, "web", "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=output, stderr=subprocess.STDOUT,
        )


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


async def _wait_ready(
    adapter: DshWebSessionAdapter, proc: subprocess.Popen, log: Path,
    timeout: float = 30,
) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
            raise AssertionError(
                f"isolated DSH exited with {proc.returncode}: {detail}")
        try:
            return await adapter._request("session.list", {})
        except Exception as exc:  # startup transport/protocol boundary
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(f"isolated DSH did not become ready: {last_error}")


async def test_dsh_and_adapter_restart_resume_same_native_session(
        tmp_path, monkeypatch):
    dsh = shutil.which("dsh")
    if dsh is None:
        pytest.skip("dsh CLI not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LAS_WORKSPACE", str(workspace))
    dsh_home = tmp_path / "dsh-home"
    log = tmp_path / "dsh-web.log"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc: subprocess.Popen | None = None
    first: DshWebSessionAdapter | None = None
    resumed: DshWebSessionAdapter | None = None
    try:
        proc = _start_dsh(dsh, dsh_home, port, log)
        first = DshWebSessionAdapter(
            base_url=base_url, event_stream=False, timeout_seconds=10)
        await _wait_ready(first, proc, log)
        created = await first._request(
            "session.create", {"cwd": str(workspace)})
        native = created.get("sessionId")
        assert isinstance(native, str) and native
        task = A2aTask(
            id="T-DSH-RESTART", status_state="submitted",
            objective="restart probe", session_id="S-before",
            native_session_id=native,
        )
        before = await first.start_session(
            task, session_id="S-before",
            metadata={"nativeSessionId": native})
        assert before.native_session_id == native
        await first.close()
        first = None

        _stop(proc)
        proc = _start_dsh(dsh, dsh_home, port, log)
        resumed = DshWebSessionAdapter(
            base_url=base_url, event_stream=False, timeout_seconds=10)
        listed = await _wait_ready(resumed, proc, log)
        assert native in {
            item.get("sessionId") for item in listed.get("items", [])
            if isinstance(item, dict)
        }
        after = await resumed.start_session(
            task, session_id="S-after",
            metadata={"nativeSessionId": native})
        assert after.native_session_id == native
        assert isinstance(await resumed._history(native), list)
    finally:
        if first is not None:
            await first.close()
        if resumed is not None:
            await resumed.close()
        _stop(proc)
