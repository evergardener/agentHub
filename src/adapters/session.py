"""Uniform multi-turn Session Adapter SDK.

The control plane owns approval and durable collaboration state.  An adapter
only translates a task/session into a native agent runtime and reports events
and artifacts.  Capabilities are explicit so callers never infer that a
one-shot runner can resume a native session.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from adapters.common import A2aTask


class SessionCapabilityError(RuntimeError):
    """Raised when a requested session control is not supported."""


@dataclass(frozen=True)
class SessionCapabilities:
    multi_turn: bool = False
    resume: bool = False
    native_resume: bool = False
    durable_session: bool = False
    streaming: bool = False
    pause: bool = False
    interrupt: bool = False
    cancel: bool = False
    interactions: bool = False
    steer: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass
class SessionHandle:
    session_id: str
    task_id: str
    native_session_id: str | None = None
    status: str = "active"
    context_revision: int = 1


@dataclass(frozen=True)
class SessionMessage:
    message_id: str
    role: str
    content: str
    based_on_revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionEvent:
    event_type: str
    session_id: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingInteraction:
    """One native agent request that requires an authoritative response."""

    interaction_id: str
    kind: str
    session_id: str
    task_id: str
    native_request_id: str | None
    native_session_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    responded_by: str | None = None
    response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "interactionId": self.interaction_id,
            "kind": self.kind,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "nativeRequestId": self.native_request_id,
            "nativeSessionId": self.native_session_id,
            "payload": self.payload,
            "status": self.status,
            "respondedBy": self.responded_by,
            "response": self.response,
        }


@dataclass
class SessionTurnResult:
    state: str
    artifacts: list[dict] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)


class SessionAdapter(ABC):
    """Adapter contract used by the shared A2A server."""

    capabilities = SessionCapabilities()

    @abstractmethod
    async def start_session(
        self, task: A2aTask, *, session_id: str, metadata: dict[str, Any]
    ) -> SessionHandle:
        """Create/bind a session without performing the first task turn."""

    @abstractmethod
    async def send_message(
        self, session_id: str, message: SessionMessage
    ) -> SessionTurnResult:
        """Send one ordered turn to an existing session."""

    @abstractmethod
    def get_session(self, session_id: str) -> SessionHandle | None:
        """Return live handle state for polling metadata; never create one."""

    async def stream_events(self, session_id: str) -> AsyncIterator[SessionEvent]:
        if not self.capabilities.streaming:
            raise SessionCapabilityError("streaming is not supported")
        if False:  # pragma: no cover - makes this an async generator contract
            yield SessionEvent("unused", session_id, "")

    async def resume_session(self, session_id: str) -> SessionHandle:
        raise SessionCapabilityError("session resume is not supported")

    async def pause(self, session_id: str) -> SessionHandle:
        raise SessionCapabilityError("session pause is not supported")

    async def interrupt(self, session_id: str) -> SessionHandle:
        raise SessionCapabilityError("session interrupt is not supported")

    async def cancel(self, session_id: str) -> SessionHandle:
        raise SessionCapabilityError("session cancellation is not supported")

    async def steer(
        self, session_id: str, message: SessionMessage
    ) -> SessionHandle:
        raise SessionCapabilityError("same-turn steering is not supported")

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return []

    def list_pending_interactions(
        self, session_id: str
    ) -> list[PendingInteraction]:
        if not self.capabilities.interactions:
            return []
        raise SessionCapabilityError("session interactions are not supported")

    async def respond_interaction(
        self,
        session_id: str,
        interaction_id: str,
        response: dict[str, Any],
        *,
        responded_by: str,
    ) -> SessionTurnResult:
        raise SessionCapabilityError("session interactions are not supported")

    async def continue_after_interaction(
        self, session_id: str
    ) -> SessionTurnResult:
        raise SessionCapabilityError(
            "interaction continuation is not supported")

    async def start(self) -> None:
        """Start optional adapter background services."""

    async def close(self) -> None:
        """Stop optional adapter background services."""


RunnerFn = Callable[[A2aTask], Awaitable[list[dict]]]


class RunnerSessionAdapter(SessionAdapter):
    """Compatibility wrapper for existing one-shot Codex/Kimi runners.

    It intentionally advertises no multi-turn or resume capability.  This
    prevents a process-local task ID from being mistaken for a native session.
    """

    capabilities = SessionCapabilities()

    def __init__(self, runner: RunnerFn):
        self._runner = runner
        self._handles: dict[str, SessionHandle] = {}
        self._tasks: dict[str, A2aTask] = {}
        self._used: set[str] = set()
        self._artifacts: dict[str, list[dict]] = {}

    async def start_session(
        self, task: A2aTask, *, session_id: str, metadata: dict[str, Any]
    ) -> SessionHandle:
        handle = SessionHandle(session_id=session_id, task_id=task.id)
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        return handle

    async def send_message(
        self, session_id: str, message: SessionMessage
    ) -> SessionTurnResult:
        if session_id not in self._handles:
            raise KeyError(f"session not found: {session_id}")
        if session_id in self._used:
            raise SessionCapabilityError(
                "one-shot runner does not support a second message")
        self._used.add(session_id)
        task = self._tasks[session_id]
        task.objective = message.content
        artifacts = await self._runner(task)
        self._artifacts[session_id] = artifacts
        self._handles[session_id].status = "completed"
        return SessionTurnResult(state="completed", artifacts=artifacts)

    def get_session(self, session_id: str) -> SessionHandle | None:
        return self._handles.get(session_id)

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._artifacts.get(session_id, []))
