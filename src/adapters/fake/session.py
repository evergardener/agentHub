"""In-process multi-turn adapter used to verify the Session SDK contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from adapters.common import A2aTask, save_artifact
from adapters.session import (
    SessionAdapter,
    SessionCapabilities,
    SessionCapabilityError,
    SessionHandle,
    SessionMessage,
    SessionTurnResult,
)
from adapters.fake.runner import FAKE_LATENCY_SECONDS


@dataclass
class _FakeSession:
    handle: SessionHandle
    task: A2aTask
    interactive: bool
    messages: list[SessionMessage] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)


class FakeSessionAdapter(SessionAdapter):
    """A deterministic adapter with pause/resume/cancel and ordered turns.

    State is deliberately process-local, therefore ``durable_session`` and
    ``native_resume`` remain false.  Durable binding belongs to the control
    plane and real runtime adapters must prove their native resume support.
    """

    capabilities = SessionCapabilities(
        multi_turn=True,
        resume=True,
        native_resume=False,
        durable_session=False,
        pause=True,
        interrupt=True,
        cancel=True,
    )

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}

    def _get(self, session_id: str) -> _FakeSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"session not found: {session_id}") from exc

    async def start_session(
        self, task: A2aTask, *, session_id: str, metadata: dict[str, Any]
    ) -> SessionHandle:
        handle = SessionHandle(session_id=session_id, task_id=task.id)
        self._sessions[session_id] = _FakeSession(
            handle=handle,
            task=task,
            interactive=bool(metadata.get("interactive")),
        )
        return handle

    async def send_message(
        self, session_id: str, message: SessionMessage
    ) -> SessionTurnResult:
        session = self._get(session_id)
        if session.handle.status in {"completed", "canceled"}:
            raise SessionCapabilityError(
                f"session is {session.handle.status}")
        if session.handle.status == "paused":
            raise SessionCapabilityError("session is paused")
        session.messages.append(message)
        await asyncio.sleep(FAKE_LATENCY_SECONDS)

        complete = (not session.interactive
                    or bool(message.metadata.get("completeSession")))
        if not complete:
            session.handle.status = "input-required"
            return SessionTurnResult(state="input-required")

        content = [
            "# Fake Multi-turn Session Result",
            "",
            f"- task_id: {session.task.id}",
            f"- session_id: {session.handle.session_id}",
            f"- turns: {len(session.messages)}",
            "",
        ]
        content.extend(
            f"{index}. [{item.role}] {item.content}"
            for index, item in enumerate(session.messages, start=1)
        )
        artifact = save_artifact(
            session.task.id,
            "result.md",
            ("\n".join(content) + "\n").encode("utf-8"),
            artifact_type="report",
        )
        session.artifacts = [artifact]
        session.handle.status = "completed"
        return SessionTurnResult(state="completed", artifacts=[artifact])

    def get_session(self, session_id: str) -> SessionHandle | None:
        session = self._sessions.get(session_id)
        return session.handle if session else None

    async def resume_session(self, session_id: str) -> SessionHandle:
        session = self._get(session_id)
        if session.handle.status != "paused":
            raise SessionCapabilityError(
                f"session is not paused: {session.handle.status}")
        session.handle.status = "input-required"
        return session.handle

    async def pause(self, session_id: str) -> SessionHandle:
        session = self._get(session_id)
        if session.handle.status in {"completed", "canceled"}:
            raise SessionCapabilityError(
                f"session is {session.handle.status}")
        session.handle.status = "paused"
        return session.handle

    async def interrupt(self, session_id: str) -> SessionHandle:
        return await self.pause(session_id)

    async def cancel(self, session_id: str) -> SessionHandle:
        session = self._get(session_id)
        if session.handle.status == "completed":
            raise SessionCapabilityError("session is completed")
        session.handle.status = "canceled"
        return session.handle

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._get(session_id).artifacts)
