"""Real Kimi ACP transport checks that do not invoke the language model."""

from __future__ import annotations

import os
import shutil

import pytest

from adapters.common import A2aTask
from adapters.kimi.session import KimiSessionAdapter

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_KIMI_ACP") != "1",
        reason="set LAS_RUN_KIMI_ACP=1 for real local ACP handshake",
    ),
    pytest.mark.skipif(not shutil.which("kimi"), reason="kimi CLI not installed"),
]


async def test_real_kimi_acp_initialize_and_session_new(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    adapter = KimiSessionAdapter(timeout_seconds=10)
    task = A2aTask(
        id="T-KIMI-ACP-PROBE",
        status_state="submitted",
        objective="transport probe only",
        session_id="S-KIMI-ACP-PROBE",
    )
    try:
        await adapter.start()
        handle = await adapter.start_session(
            task, session_id=task.session_id, metadata={})
        assert handle.native_session_id
        assert handle.status == "active"
    finally:
        await adapter.close()
