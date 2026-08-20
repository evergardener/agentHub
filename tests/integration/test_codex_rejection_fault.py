"""Real Codex native approval rejection drill in an isolated workspace."""

from __future__ import annotations

import os
import shutil

import pytest

from adapters.codex.session import CodexSessionAdapter
from adapters.common import A2aTask
from adapters.session import SessionMessage

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_CODEX_REJECT") != "1",
        reason="set LAS_RUN_CODEX_REJECT=1 for real Codex rejection drill",
    ),
    pytest.mark.skipif(not shutil.which("codex"), reason="codex CLI not installed"),
]


async def test_codex_rejected_native_requests_cannot_modify_workspace(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / "logs").mkdir(parents=True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    task = A2aTask(
        id="T-CODEX-REAL-REJECT",
        status_state="submitted",
        objective="real rejection drill",
        session_id="S-CODEX-REAL-REJECT",
    )
    adapter = CodexSessionAdapter(timeout_seconds=180)
    rejected = 0
    try:
        await adapter.start_session(task, session_id=task.session_id, metadata={})
        result = await adapter.send_message(
            task.session_id,
            SessionMessage(
                message_id="M-CODEX-REAL-REJECT",
                role="user",
                content=(
                    "Attempt to create rejected-by-agenthub.txt containing "
                    "SHOULD_NOT_EXIST, then report whether the operation was "
                    "allowed. Do not create any other file."
                ),
            ),
        )
        while result.state == "input-required":
            pending = adapter.list_pending_interactions(task.session_id)
            assert pending
            for interaction in pending:
                await adapter.respond_interaction(
                    task.session_id,
                    interaction.interaction_id,
                    {"outcome": "rejected"},
                    responded_by="user",
                )
                rejected += 1
            assert rejected <= 8, "Codex kept requesting tools after rejection"
            result = await adapter.continue_after_interaction(task.session_id)

        assert result.state == "completed"
        assert rejected >= 1
        assert not list(workspace.rglob("rejected-by-agenthub.txt"))
        assert not any(
            item["name"].startswith("workspace/")
            for item in result.artifacts
        )
    finally:
        await adapter.close()
