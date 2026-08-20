"""Persistent DeepSeek Harness Web API Session Adapter.

DSH 0.1.0-rc.6 ships two relevant surfaces:

* ``headless`` creates a fresh persisted session for one task and exits.  It
  has no resume argument and therefore must not back a durable AgentHub
  session.
* ``web`` exposes the native session API used here.  It supports explicit
  session creation, history, additional prompts, cancellation, and durable
  recovery by DSH session id.

The adapter deliberately talks to the loopback Web API instead of importing
DSH's private Node modules.  This keeps DSH independently deployable and makes
the integration an ordinary, version-testable protocol boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from adapters.common import A2aTask, save_artifact, workspace_root
from adapters.dsh.safety import (
    normalize_tool_view,
    redact_bounded,
    safe_tool_view,
    tool_view_is_inspectable,
)
from adapters.session import (
    PendingInteraction,
    SessionAdapter,
    SessionCapabilities,
    SessionCapabilityError,
    SessionEvent,
    SessionHandle,
    SessionMessage,
    SessionTurnResult,
)


DEFAULT_DSH_WEB_URL = "http://127.0.0.1:3080"
DEFAULT_TIMEOUT_SECONDS = 3600
SAFE_PERMISSION_PRESETS = frozenset({"read-only"})


class DshNotAvailable(RuntimeError):
    """The configured DSH Web service cannot be reached."""


class DshApiError(RuntimeError):
    """DSH returned a transport, protocol, or business error."""


class DshTurnFailed(RuntimeError):
    """A DSH turn ended with a native error."""


class DshTimeout(RuntimeError):
    """A DSH turn did not settle within the configured timeout."""


def _event(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("event", entry)
    return value if isinstance(value, dict) else {}


def _seq(entry: dict[str, Any]) -> int:
    value = _event(entry).get("seq", -1)
    return value if isinstance(value, int) else -1


def _assistant_text(entries: list[dict[str, Any]], after_seq: int) -> str:
    messages: list[str] = []
    for entry in entries:
        event = _event(entry)
        if _seq(entry) <= after_seq or event.get("type") != "assistant/message":
            continue
        message = event.get("data", {}).get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                messages.append(block["text"])
    return "\n\n".join(part for part in messages if part.strip())


def _turn_end(entries: list[dict[str, Any]], after_seq: int) -> dict | None:
    for entry in reversed(entries):
        event = _event(entry)
        if _seq(entry) > after_seq and event.get("type") == "turn/end":
            return event
    return None


def _pending_approval(entries: list[dict[str, Any]], after_seq: int) -> dict | None:
    decided: set[str] = set()
    asked: list[dict] = []
    for entry in entries:
        event = _event(entry)
        if _seq(entry) <= after_seq:
            continue
        data = event.get("data", {})
        if event.get("type") == "approval/decided" and data.get("id"):
            decided.add(data["id"])
        elif event.get("type") == "approval/asked":
            asked.append(event)
    return next(
        (event for event in reversed(asked)
         if event.get("data", {}).get("id") not in decided),
        None,
    )


def _approval_outcome(
    entries: list[dict[str, Any]], approval_id: str | None,
) -> str | None:
    if not approval_id:
        return None
    for entry in reversed(entries):
        event = _event(entry)
        data = event.get("data") or {}
        if (event.get("type") == "approval/decided"
                and data.get("id") == approval_id):
            outcome = data.get("outcome")
            return outcome if isinstance(outcome, str) else None
    return None


def _history_tool_view(
    entries: list[dict[str, Any]], call_id: str
) -> dict[str, Any] | None:
    for entry in reversed(entries):
        event = _event(entry)
        data = event.get("data") or {}
        if (event.get("type") == "tool/call"
                and data.get("callId") == call_id):
            return safe_tool_view(entry.get("view"))
    return None


class DshWebSessionAdapter(SessionAdapter):
    """Translate AgentHub sessions to DSH's durable Web session protocol."""

    capabilities = SessionCapabilities(
        multi_turn=True,
        resume=True,
        native_resume=True,
        durable_session=True,
        streaming=True,
        pause=False,
        interrupt=True,
        cancel=True,
        interactions=True,
        steer=True,
    )

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval: float = 0.25,
        permission_preset: str | None = None,
        client: httpx.AsyncClient | None = None,
        event_stream: bool | None = None,
        interaction_wait_seconds: float = 2.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("LAS_DSH_WEB_URL", DEFAULT_DSH_WEB_URL)
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.permission_preset = permission_preset or os.environ.get(
            "LAS_DSH_PERMISSION_PRESET", "read-only")
        if self.permission_preset not in SAFE_PERMISSION_PRESETS:
            raise ValueError(
                "LAS_DSH_PERMISSION_PRESET must remain read-only; modifying "
                "calls use one ActionIntent-bound allowed-once response")
        self._client = client
        self._handles: dict[str, SessionHandle] = {}
        self._tasks: dict[str, A2aTask] = {}
        self._artifacts: dict[str, list[dict]] = {}
        self._last_seq: dict[str, int] = {}
        self._event_queues: dict[str, asyncio.Queue[SessionEvent]] = {}
        self._pending_interactions: dict[
            str, dict[str, PendingInteraction]
        ] = {}
        self._native_to_session: dict[str, str] = {}
        self._turn_baselines: dict[str, int] = {}
        self._tool_call_views: dict[tuple[str, str], dict[str, Any]] = {}
        self._interaction_changed = asyncio.Event()
        self._event_stream_enabled = (
            client is None if event_stream is None else event_stream)
        self._event_stream_task: asyncio.Task | None = None
        self._event_stream_stop = asyncio.Event()
        self.interaction_wait_seconds = interaction_wait_seconds

    def get_session(self, session_id: str) -> SessionHandle | None:
        return self._handles.get(session_id)

    async def start(self) -> None:
        await self._ensure_event_stream()

    async def close(self) -> None:
        self._event_stream_stop.set()
        if self._event_stream_task is not None:
            self._event_stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._event_stream_task
            self._event_stream_task = None

    async def _ensure_event_stream(self) -> None:
        if not self._event_stream_enabled:
            return
        if self._event_stream_task is None or self._event_stream_task.done():
            self._event_stream_stop.clear()
            self._event_stream_task = asyncio.create_task(
                self._event_stream_loop())

    async def _event_stream_loop(self) -> None:
        delay = 0.25
        while not self._event_stream_stop.is_set():
            try:
                await self._consume_event_stream()
                delay = 0.25
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect boundary
                for session_id in tuple(self._handles):
                    await self._emit(
                        session_id, "dsh.stream.disconnected",
                        {"error": str(exc), "retrySeconds": delay},
                    )
                try:
                    await asyncio.wait_for(
                        self._event_stream_stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 5.0)

    async def _consume_event_stream(self) -> None:
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, timeout=None)
        try:
            async with client.stream("GET", "/api/events.mux") as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if self._event_stream_stop.is_set():
                        return
                    if not line.startswith("data:"):
                        continue
                    try:
                        message = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict):
                        await self.ingest_server_request(message)
        finally:
            if owned:
                await client.aclose()

    async def ingest_server_request(self, message: dict[str, Any]) -> None:
        """Ingest one DSH mux ServerRequest; public for protocol testing."""
        if message.get("type") != "server-request":
            return
        rpc_id = message.get("rpcId")
        payload = message.get("payload")
        if not isinstance(rpc_id, str) or not isinstance(payload, dict):
            return
        method = message.get("method")
        if method != payload.get("type"):
            return
        native = payload.get("sessionId")
        session_id = self._native_to_session.get(str(native))
        if session_id is None:
            return

        if method in {"approval/requested", "question/requested"}:
            kind = "approval" if method == "approval/requested" else "question"
            interaction_id = f"dsh:{rpc_id}"
            interactions = self._pending_interactions.setdefault(session_id, {})
            if kind == "approval":
                approval_id = payload.get("approvalId")
                for old_id, old in tuple(interactions.items()):
                    if (old.kind == "approval"
                            and old.payload.get("approvalId") == approval_id
                            and old.native_request_id is None):
                        interactions.pop(old_id)
            interaction = interactions.get(interaction_id)
            if interaction is None:
                handle = self._handles[session_id]
                interaction_payload = dict(payload)
                call_id = payload.get("callId")
                tool_view = self._tool_call_views.get(
                    (str(native), str(call_id))) if call_id else None
                if kind == "approval" and call_id and tool_view is None:
                    try:
                        tool_view = _history_tool_view(
                            await self._history(str(native)), str(call_id))
                    except (DshApiError, DshNotAvailable):
                        tool_view = None
                if kind == "approval":
                    tool_view = normalize_tool_view(
                        tool_view,
                        workspace=self._workspace(handle.task_id),
                    )
                    interaction_payload["inspectable"] = \
                        tool_view_is_inspectable(tool_view)
                    if tool_view is not None:
                        interaction_payload["toolView"] = tool_view
                interaction = PendingInteraction(
                    interaction_id=interaction_id,
                    kind=kind,
                    session_id=session_id,
                    task_id=handle.task_id,
                    native_request_id=rpc_id,
                    native_session_id=str(native),
                    payload=interaction_payload,
                )
                interactions[interaction_id] = interaction
                await self._emit(
                    session_id, f"{kind}.requested",
                    {"interaction": interaction.to_dict()},
                )
            self._interaction_changed.set()
            return

        if method == "session/event":
            event = payload.get("event")
            if isinstance(event, dict) and event.get("type") == "tool/call":
                data = event.get("data") or {}
                call_id = data.get("callId")
                safe_view = normalize_tool_view(
                    safe_tool_view(payload.get("view")),
                    workspace=self._workspace(
                        self._handles[session_id].task_id),
                )
                if call_id and safe_view is not None:
                    self._tool_call_views[(str(native), str(call_id))] = safe_view
                    for interaction in self._pending_interactions.get(
                            session_id, {}).values():
                        if (interaction.status == "pending"
                                and interaction.kind == "approval"
                                and interaction.payload.get("callId") == call_id):
                            interaction.payload["toolView"] = safe_view
                            interaction.payload["inspectable"] = \
                                tool_view_is_inspectable(safe_view)
                            self._interaction_changed.set()

        if method in {"approval/resolved", "question/resolved"}:
            interactions = self._pending_interactions.get(session_id, {})
            for interaction in interactions.values():
                matches = (
                    method == "approval/resolved"
                    and interaction.kind == "approval"
                    and interaction.payload.get("approvalId")
                    == payload.get("approvalId")
                ) or (
                    method == "question/resolved"
                    and interaction.kind == "question"
                    and interaction.native_request_id
                    == payload.get("questionRpcId")
                )
                if matches and interaction.status == "pending":
                    interaction.status = "resolved"
                    if interaction.kind == "approval":
                        interaction.response = {
                            "outcome": payload.get("outcome"),
                            "nativeResolution": True,
                        }
                    elif interaction.kind == "question":
                        interaction.response = {
                            "answer": payload.get("answer"),
                            "nativeResolution": True,
                        }
            self._interaction_changed.set()

        event_type = method or "event"
        await self._emit(session_id, f"dsh.{event_type}", payload)

    async def health(self) -> dict[str, Any]:
        value = await self._request("session.list", {})
        items = value.get("items")
        if not isinstance(items, list):
            raise DshApiError("DSH session.list returned invalid items")
        return {"runtime": "dsh-web", "sessions": len(items)}

    def _workspace(self, task_id: str) -> Path:
        path = workspace_root() / "tasks" / task_id
        (path / "input").mkdir(parents=True, exist_ok=True)
        (path / "logs").mkdir(parents=True, exist_ok=True)
        return path

    async def _request(self, method: str, payload: dict[str, Any]) -> dict:
        rpc_id = f"agenthub-{uuid.uuid4()}"
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": payload,
        }
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0)
        try:
            response = await client.post(
                f"/api/{method}", json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise DshNotAvailable(
                f"DSH Web API unavailable at {self.base_url}: {exc}"
            ) from exc
        finally:
            if owned:
                await client.aclose()
        if envelope.get("type") != "server-response":
            raise DshApiError(f"invalid DSH response type for {method}")
        if envelope.get("rpcId") != rpc_id:
            raise DshApiError(f"DSH rpcId mismatch for {method}")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise DshApiError(f"invalid DSH result for {method}")
        if result.get("ok") is not True:
            error = result.get("error") or {}
            raise DshApiError(
                f"DSH {method} failed: {error.get('code', 'unknown')}: "
                f"{error.get('message', 'unknown error')}"
            )
        value = result.get("value")
        return value if isinstance(value, dict) else {}

    async def _history(self, native_session_id: str) -> list[dict[str, Any]]:
        value = await self._request(
            "session.history",
            {"sessionId": native_session_id, "maxMessages": 200},
        )
        entries = value.get("events", [])
        if not isinstance(entries, list):
            raise DshApiError("DSH session.history returned invalid events")
        return [entry for entry in entries if isinstance(entry, dict)]

    async def start_session(
        self, task: A2aTask, *, session_id: str, metadata: dict[str, Any]
    ) -> SessionHandle:
        await self._ensure_event_stream()
        native = metadata.get("nativeSessionId") or task.native_session_id
        if native:
            await self._history(str(native))
        else:
            payload: dict[str, Any] = {"cwd": str(self._workspace(task.id))}
            preset = metadata.get("dshAgentPreset") or os.environ.get(
                "LAS_DSH_AGENT_PRESET")
            if preset:
                payload["agentPreset"] = str(preset)
            native = (await self._request("session.create", payload)).get(
                "sessionId")
            if not isinstance(native, str) or not native:
                raise DshApiError("DSH session.create returned no sessionId")
            permission = metadata.get(
                "dshPermissionPreset", self.permission_preset)
            if permission not in SAFE_PERMISSION_PRESETS:
                raise DshApiError(
                    "DSH permission preset must remain read-only; modifying "
                    "calls require one ActionIntent-bound allowed-once response")
            await self._request("session.prompt", {
                "sessionId": native,
                "mode": "queue",
                "content": [{
                    "type": "text", "text": f"/permission {permission}"}],
            })
        handle = SessionHandle(
            session_id=session_id,
            task_id=task.id,
            native_session_id=str(native),
            status="active",
            context_revision=task.context_revision,
        )
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        self._event_queues.setdefault(session_id, asyncio.Queue())
        self._pending_interactions.setdefault(session_id, {})
        self._native_to_session[str(native)] = session_id
        return handle

    async def _emit(
        self, session_id: str, event_type: str, payload: dict[str, Any]
    ) -> SessionEvent:
        handle = self._handles[session_id]
        event = SessionEvent(
            event_type=event_type,
            session_id=session_id,
            task_id=handle.task_id,
            payload=redact_bounded(payload),
        )
        await self._event_queues[session_id].put(event)
        return event

    async def send_message(
        self, session_id: str, message: SessionMessage
    ) -> SessionTurnResult:
        handle = self._handles.get(session_id)
        task = self._tasks.get(session_id)
        if handle is None or task is None or not handle.native_session_id:
            raise KeyError(f"session not found: {session_id}")
        if handle.status == "canceled":
            raise SessionCapabilityError("session is canceled")

        native = handle.native_session_id
        before = await self._history(native)
        baseline = max((_seq(entry) for entry in before), default=-1)
        self._turn_baselines[session_id] = baseline
        await self._request(
            "session.prompt",
            {
                "sessionId": native,
                "mode": "steer" if message.metadata.get("steer") else "queue",
                "content": [{"type": "text", "text": message.content}],
            },
        )
        handle.status = "working"
        emitted: list[SessionEvent] = [await self._emit(
            session_id, "dsh.turn.started",
            {"nativeSessionId": native, "afterSeq": baseline},
        )]
        return await self._wait_for_turn(
            session_id, task, baseline, before, emitted)

    async def _wait_for_turn(
        self,
        session_id: str,
        task: A2aTask,
        baseline: int,
        entries: list[dict[str, Any]] | None = None,
        emitted: list[SessionEvent] | None = None,
    ) -> SessionTurnResult:
        handle = self._handles[session_id]
        native = handle.native_session_id
        if not native:
            raise SessionCapabilityError("native DSH session ID is missing")
        emitted = emitted if emitted is not None else []
        deadline = time.monotonic() + self.timeout_seconds
        entries = entries or []
        while time.monotonic() < deadline:
            entries = await self._history(native)
            latest = max((_seq(entry) for entry in entries), default=-1)
            previous = self._last_seq.get(session_id, baseline)
            for entry in entries:
                event = _event(entry)
                if _seq(entry) <= max(previous, baseline):
                    continue
                emitted.append(await self._emit(
                    session_id, f"dsh.{event.get('type', 'event')}",
                    {"event": event},
                ))
            self._last_seq[session_id] = max(previous, latest)

            pending = self.list_pending_interactions(session_id)
            if pending:
                handle.status = "input-required"
                artifacts = self._save_turn_artifacts(
                    task, entries, baseline, state="input-required")
                return SessionTurnResult(
                    state="input-required", artifacts=artifacts,
                    events=emitted)

            approval = _pending_approval(entries, baseline)
            if approval is not None:
                approval_id = approval.get("data", {}).get("id")
                if self._approval_was_responded(session_id, approval_id):
                    await asyncio.sleep(self.poll_interval)
                    continue
                interaction = await self._wait_for_native_approval(
                    session_id, native, approval)
                handle.status = "input-required"
                if interaction.native_request_id is None:
                    emitted.append(await self._emit(
                        session_id, "approval.requested",
                        {"interaction": interaction.to_dict()},
                    ))
                    emitted.append(await self._emit(
                        session_id, "approval.bridge_unavailable",
                        {"interaction": interaction.to_dict()},
                    ))
                artifacts = self._save_turn_artifacts(
                    task, entries, baseline, state="input-required")
                return SessionTurnResult(
                    state="input-required", artifacts=artifacts, events=emitted)

            ended = _turn_end(entries, baseline)
            if ended is not None:
                reason = ended.get("data", {}).get("reason", {})
                kind = reason.get("kind")
                if kind == "error":
                    error = reason.get("error", {})
                    raise DshTurnFailed(
                        f"DSH turn failed: {error.get('code', 'UNKNOWN')}: "
                        f"{error.get('message', 'unknown error')}"
                    )
                state = "completed"
                if kind == "blocked":
                    state = "input-required"
                elif kind in {"aborted", "interrupted"}:
                    state = "canceled"
                handle.status = state
                artifacts = self._save_turn_artifacts(
                    task, entries, baseline, state=state)
                return SessionTurnResult(
                    state=state, artifacts=artifacts, events=emitted)
            await asyncio.sleep(self.poll_interval)
        raise DshTimeout(
            f"DSH session {native} did not settle in "
            f"{self.timeout_seconds:g}s")

    def _approval_was_responded(
        self, session_id: str, approval_id: str | None
    ) -> bool:
        return any(
            interaction.kind == "approval"
            and interaction.payload.get("approvalId") == approval_id
            and interaction.status in {"responded", "resolved"}
            for interaction in self._pending_interactions.get(
                session_id, {}).values()
        )

    async def _wait_for_native_approval(
        self, session_id: str, native: str, approval: dict[str, Any]
    ) -> PendingInteraction:
        approval_id = approval.get("data", {}).get("id")

        def find() -> PendingInteraction | None:
            return next((
                item for item in self._pending_interactions.get(
                    session_id, {}).values()
                if item.kind == "approval"
                and item.payload.get("approvalId") == approval_id
                and item.status == "pending"
            ), None)

        interaction = find()
        deadline = time.monotonic() + self.interaction_wait_seconds
        while interaction is None and time.monotonic() < deadline:
            self._interaction_changed.clear()
            try:
                await asyncio.wait_for(
                    self._interaction_changed.wait(),
                    timeout=max(0, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
            interaction = find()
        if interaction is not None:
            return interaction

        interaction = PendingInteraction(
            interaction_id=f"dsh-approval:{approval_id or uuid.uuid4()}",
            kind="approval",
            session_id=session_id,
            task_id=self._handles[session_id].task_id,
            native_request_id=None,
            native_session_id=native,
            payload={
                "type": "approval/requested",
                "sessionId": native,
                "approvalId": approval_id,
                "toolName": approval.get("data", {}).get("toolName"),
                "reason": approval.get("data", {}).get("reason"),
                "respondable": False,
                "inspectable": False,
            },
        )
        self._pending_interactions.setdefault(session_id, {})[
            interaction.interaction_id] = interaction
        return interaction

    def list_pending_interactions(
        self, session_id: str
    ) -> list[PendingInteraction]:
        if session_id not in self._handles:
            raise KeyError(f"session not found: {session_id}")
        return [
            item for item in self._pending_interactions.get(
                session_id, {}).values()
            if item.status == "pending"
        ]

    async def _post_response(
        self, native_request_id: str, value: dict[str, Any]
    ) -> None:
        body = {
            "type": "client-response",
            "rpcId": native_request_id,
            "result": {"ok": True, "value": value},
        }
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0)
        try:
            response = await client.post(
                "/api/respond", json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            receipt = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise DshNotAvailable(
                f"DSH response endpoint unavailable at {self.base_url}: {exc}"
            ) from exc
        finally:
            if owned:
                await client.aclose()
        if receipt.get("accepted") is not True:
            raise DshApiError(
                f"DSH rejected interaction response: "
                f"{receipt.get('reason', 'unknown')}")

    async def respond_interaction(
        self,
        session_id: str,
        interaction_id: str,
        response: dict[str, Any],
        *,
        responded_by: str,
    ) -> SessionTurnResult:
        if responded_by not in {"user", "hermes"}:
            raise PermissionError(
                "only user or hermes may respond to agent interactions")
        interaction = self._pending_interactions.get(
            session_id, {}).get(interaction_id)
        if interaction is None:
            raise KeyError(f"interaction not found: {interaction_id}")
        if interaction.status in {"responded", "resolved"}:
            previous = interaction.response or {}
            if interaction.kind == "approval":
                matches = previous.get("outcome") == response.get("outcome")
            else:
                matches = previous.get("answer") == response.get("answer")
            if not matches:
                raise SessionCapabilityError(
                    "interaction was resolved with a different response")
            return SessionTurnResult(state="working")
        if interaction.status != "pending":
            raise SessionCapabilityError(
                f"interaction is already {interaction.status}")
        if not interaction.native_request_id:
            raise SessionCapabilityError(
                "native DSH rpcId unavailable; reconnect the event stream")
        native = interaction.native_session_id
        if not native:
            raise SessionCapabilityError("native DSH session ID is missing")

        if interaction.kind == "approval":
            outcome = response.get("outcome")
            if outcome not in {"allowed-once", "rejected"}:
                raise ValueError(
                    "approval outcome must be allowed-once or rejected")
            if outcome == "allowed-once":
                if interaction.payload.get("inspectable") is not True:
                    raise PermissionError(
                        "approval details are incomplete; only rejection is safe")
                authorization = response.get("authorization") or {}
                if (authorization.get("status") != "approved"
                        or authorization.get("decidedBy")
                        not in {"user", "hermes"}
                        or not authorization.get("actionIntentId")):
                    raise PermissionError(
                        "allowed-once requires an approved ActionIntent receipt")
                expected_claims = {
                    "taskId": interaction.task_id,
                    "interactionId": interaction.interaction_id,
                    "nativeRequestId": interaction.native_request_id,
                }
                if any(authorization.get(key) != value
                       for key, value in expected_claims.items()):
                    raise PermissionError(
                        "ActionIntent receipt does not match this interaction")
                from common.action_receipt import verify_action_receipt

                if not verify_action_receipt(authorization):
                    raise PermissionError(
                        "ActionIntent receipt signature is invalid")
            value = {
                "sessionId": native,
                "approvalId": interaction.payload.get("approvalId"),
                "outcome": outcome,
            }
        elif interaction.kind == "question":
            answer = response.get("answer")
            if not isinstance(answer, dict) or not isinstance(
                    answer.get("answers"), list):
                raise ValueError(
                    "question response requires answer.answers")
            value = {"sessionId": native, "answer": answer}
        else:  # pragma: no cover - guarded by ingest
            raise ValueError(f"unsupported interaction kind: {interaction.kind}")

        native_applied = False
        if interaction.kind == "approval":
            try:
                decided = _approval_outcome(
                    await self._history(native),
                    interaction.payload.get("approvalId"),
                )
            except (DshApiError, DshNotAvailable):
                decided = None
            if decided is not None and decided != response.get("outcome"):
                raise SessionCapabilityError(
                    "native DSH approval has a conflicting outcome")
            native_applied = decided == response.get("outcome")
        if not native_applied:
            await self._post_response(interaction.native_request_id, value)
        interaction.status = "responded"
        interaction.responded_by = responded_by
        interaction.response = dict(response)
        self._interaction_changed.set()
        handle = self._handles[session_id]
        handle.status = "working"
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise SessionCapabilityError("no active DSH turn to continue")
        return SessionTurnResult(state="working")

    async def continue_after_interaction(
        self, session_id: str
    ) -> SessionTurnResult:
        baseline = self._turn_baselines.get(session_id)
        if baseline is None:
            raise SessionCapabilityError("no active DSH turn to continue")
        return await self._wait_for_turn(
            session_id, self._tasks[session_id], baseline)

    def _save_turn_artifacts(
        self, task: A2aTask, entries: list[dict[str, Any]],
        baseline: int, *, state: str,
    ) -> list[dict]:
        encoded = json.dumps(
            redact_bounded({
                "state": state, "afterSeq": baseline, "entries": entries,
            }),
            ensure_ascii=False, indent=2,
        ).encode("utf-8")
        artifacts = [save_artifact(
            task.id, "dsh-history.json", encoded, "log")]
        answer = _assistant_text(entries, baseline)
        if answer:
            answer = str(redact_bounded(answer))
            artifacts.append(save_artifact(
                task.id, "last-message.md", answer.encode("utf-8"), "report"))
        artifacts.extend(self._workspace_artifacts(task.id))
        self._artifacts[task.session_id or task.id] = artifacts
        return artifacts

    def _workspace_artifacts(self, task_id: str) -> list[dict]:
        root = self._workspace(task_id)
        artifacts: list[dict] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel.parts[0] in {"artifacts", "input", "logs"}:
                continue
            artifacts.append(save_artifact(
                task_id, f"workspace/{rel}", path.read_bytes(), "file"))
        return artifacts

    async def stream_events(
        self, session_id: str
    ) -> AsyncIterator[SessionEvent]:
        if session_id not in self._event_queues:
            raise KeyError(f"session not found: {session_id}")
        queue = self._event_queues[session_id]
        while True:
            yield await queue.get()

    async def resume_session(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native DSH session ID is missing")
        await self._history(handle.native_session_id)
        handle.status = "active"
        return handle

    async def steer(self, session_id: str,
                    message: SessionMessage) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native DSH session ID is missing")
        await self._request("session.prompt", {
            "sessionId": handle.native_session_id,
            "mode": "steer",
            "content": [{"type": "text", "text": message.content}],
        })
        handle.context_revision = message.based_on_revision
        await self._emit(session_id, "user.steer", {
            "messageId": message.message_id,
            "contextRevision": message.based_on_revision,
            "text": message.content,
        })
        return handle

    async def interrupt(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native DSH session ID is missing")
        await self._request(
            "session.cancel", {"sessionId": handle.native_session_id})
        handle.status = "paused"
        return handle

    async def cancel(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native DSH session ID is missing")
        await self._request(
            "session.cancel", {"sessionId": handle.native_session_id})
        handle.status = "canceled"
        return handle

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._artifacts.get(session_id, []))
