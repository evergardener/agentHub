"""Opt-in real DSH WebSocket approval gate for the audited standard preset."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapters.common import A2aTask
from adapters.dsh.session import DshWebSessionAdapter
from adapters.session import SessionMessage
from common.action_receipt import sign_action_receipt

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("LAS_RUN_DSH_APPROVAL") != "1",
        reason="set LAS_RUN_DSH_APPROVAL=1 for the real DSH approval gate",
    ),
]


def _authorization(task: A2aTask, interaction, native: str) -> dict:
    return sign_action_receipt({
        "actionIntentId": f"AI-{interaction.interaction_id}",
        "status": "approved",
        "decidedBy": "hermes",
        "decidedAt": "real-gate",
        "basedOnRevision": 1,
        "taskId": task.id,
        "interactionId": interaction.interaction_id,
        "nativeRequestId": interaction.native_request_id,
        "nativeSessionId": native,
        "contextRevision": 1,
    })


async def test_real_dsh_reject_then_allow_once_same_native_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "real-dsh-gate-secret-0123456789")
    adapter = DshWebSessionAdapter(
        base_url=os.environ.get("LAS_DSH_WEB_URL", "http://127.0.0.1:3080"),
        timeout_seconds=180,
        poll_interval=0.1,
        interaction_wait_seconds=2,
        event_stream=True,
    )
    task = A2aTask(
        id="T-DSH-APPROVAL-REAL",
        status_state="submitted",
        objective="verify native modification approval",
        session_id="S-DSH-APPROVAL-REAL",
    )
    workspace = tmp_path / "tasks" / task.id
    rejected_path = workspace / "rejected-proof.txt"
    allowed_path = workspace / "allowed-proof.txt"
    try:
        handle = await adapter.start_session(
            task, session_id=task.session_id,
            metadata={"dshAgentPreset": "standard"})

        rejected_turn = await adapter.send_message(
            task.session_id,
            SessionMessage(
                "M-reject", "user",
                "Create rejected-proof.txt containing REJECTED. Use write.",
            ),
        )
        assert rejected_turn.state == "input-required"
        rejected = adapter.list_pending_interactions(task.session_id)[0]
        assert rejected.payload["inspectable"] is True
        assert not rejected_path.exists()
        response = await adapter.respond_interaction(
            task.session_id, rejected.interaction_id,
            {"outcome": "rejected"}, responded_by="hermes")
        assert response.state == "working"
        assert (await adapter.continue_after_interaction(
            task.session_id)).state == "completed"
        assert not rejected_path.exists()

        allowed_turn = await adapter.send_message(
            task.session_id,
            SessionMessage(
                "M-allow", "user",
                "Create allowed-proof.txt containing ALLOWED. Use write.",
            ),
        )
        assert allowed_turn.state == "input-required"
        allowed = adapter.list_pending_interactions(task.session_id)[0]
        assert allowed.payload["inspectable"] is True
        assert not allowed_path.exists()
        response = await adapter.respond_interaction(
            task.session_id, allowed.interaction_id,
            {
                "outcome": "allowed-once",
                "authorization": _authorization(
                    task, allowed, str(handle.native_session_id)),
            },
            responded_by="hermes",
        )
        assert response.state == "working"
        assert (await adapter.continue_after_interaction(
            task.session_id)).state == "completed"
        assert allowed_path.read_text(encoding="utf-8").strip() == "ALLOWED"
    finally:
        await adapter.close()
