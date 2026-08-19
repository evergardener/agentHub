"""Real Codex App Server transport check without a model invocation."""

from __future__ import annotations

import os
import shutil

import pytest

from adapters.codex.session import CodexSessionAdapter
from adapters.common import A2aTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_CODEX_APP_SERVER") != "1",
        reason="set LAS_RUN_CODEX_APP_SERVER=1 for local app-server probe",
    ),
    pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed"),
]


async def test_real_codex_app_server_initialize_and_thread_start(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    adapter = CodexSessionAdapter(timeout_seconds=15)
    task = A2aTask(
        id="T-CODEX-APP-SERVER-PROBE", status_state="submitted",
        objective="transport probe only", session_id="S-CODEX-APP-SERVER-PROBE",
    )
    try:
        await adapter.start()
        handle = await adapter.start_session(
            task, session_id=task.session_id, metadata={})
        assert handle.native_session_id
        assert handle.status == "active"
        assert handle.native_session_id in adapter._loaded_threads
    finally:
        await adapter.close()
