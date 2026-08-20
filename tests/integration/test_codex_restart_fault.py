"""Real Codex two-turn resume across App Server/Adapter reconstruction.

This opt-in test invokes the configured Codex model twice. Both turns explicitly
forbid tool use and run with the Adapter's enforced read-only sandbox.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from adapters.codex.session import CodexSessionAdapter
from adapters.common import A2aTask
from adapters.session import SessionMessage

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_CODEX_RESTART") != "1",
        reason="set LAS_RUN_CODEX_RESTART=1 for real Codex resume drill",
    ),
    pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed"),
]


def _last_message(artifacts: list[dict]) -> str:
    artifact = next(item for item in artifacts if item["name"] == "last-message.md")
    return Path(artifact["path"]).read_text(encoding="utf-8")


async def test_codex_resumes_same_thread_after_adapter_reconstruction(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    task = A2aTask(
        id="T-CODEX-REAL-RESTART",
        status_state="submitted",
        objective="real read-only resume drill",
        session_id="S-CODEX-REAL-RESTART",
        context_revision=1,
    )

    first = CodexSessionAdapter(timeout_seconds=180)
    try:
        handle = await first.start_session(
            task, session_id=task.session_id, metadata={})
        turn_one = await first.send_message(
            task.session_id,
            SessionMessage(
                message_id="M-CODEX-REAL-ONE",
                role="user",
                content=(
                    "Do not call tools or modify files. Reply with exactly "
                    "CODEX_TURN_ONE_MARKER."
                ),
                based_on_revision=1,
            ),
        )
        assert turn_one.state == "completed"
        assert "CODEX_TURN_ONE_MARKER" in _last_message(turn_one.artifacts)
        native = handle.native_session_id
        assert native
    finally:
        await first.close()

    task.native_session_id = native
    task.context_revision = 2
    resumed = CodexSessionAdapter(timeout_seconds=180)
    try:
        handle = await resumed.start_session(
            task,
            session_id=task.session_id,
            metadata={"nativeSessionId": native},
        )
        assert handle.native_session_id == native
        turn_two = await resumed.send_message(
            task.session_id,
            SessionMessage(
                message_id="M-CODEX-REAL-TWO",
                role="user",
                content=(
                    "Do not call tools or modify files. State the exact marker "
                    "from my previous turn, prefixed with CODEX_RESUME_OK:."
                ),
                based_on_revision=2,
            ),
        )
        assert turn_two.state == "completed"
        summary = _last_message(turn_two.artifacts)
        assert "CODEX_RESUME_OK:" in summary
        assert "CODEX_TURN_ONE_MARKER" in summary
    finally:
        await resumed.close()
