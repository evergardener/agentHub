"""Kimi Code ACP adapter with durable sessions and native approvals.

Kimi's prompt CLI cannot expose a respondable approval request to AgentHub.
The ACP server can: ``session/request_permission`` is kept pending until the
control plane answers the matching ActionIntent. This adapter therefore uses
``kimi acp`` instead of treating a post-execution JSONL log as authorization.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from adapters.common import A2aTask, save_artifact, workspace_root
from adapters.kimi.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    KimiFailed,
    KimiTimeout,
    _find_kimi,
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
from common.action_receipt import verify_action_receipt


def _find_session_id(value) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        for nested in value.values():
            found = _find_session_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_session_id(nested)
            if found:
                return found
    return None


def extract_kimi_session_id(jsonl: str) -> str | None:
    """Compatibility parser retained for old stream-json artifacts."""
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        found = _find_session_id(event)
        if found:
            return found
    return None


def find_kimi_session_for_workspace(
        workspace: Path, index_path: Path | None = None) -> str | None:
    """Compatibility lookup used when importing an older prompt-CLI session."""
    path = index_path or (Path.home() / ".kimi-code" / "session_index.jsonl")
    if not path.exists():
        return None
    expected = {str(workspace), str(workspace.resolve())}
    latest = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("workDir") in expected:
            candidate = record.get("sessionId")
            if isinstance(candidate, str) and candidate:
                latest = candidate
    return latest


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key)", re.I)


def _bounded(value: Any, *, limit: int = 4096) -> Any:
    """Keep approval/audit payloads useful without copying unbounded data."""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_bounded(item, limit=limit) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: (
                "[REDACTED]" if _SENSITIVE_KEY.search(str(key))
                else _bounded(item, limit=limit)
            )
            for key, item in list(value.items())[:50]
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)[:limit]


class KimiSessionAdapter(SessionAdapter):
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
    )

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self._handles: dict[str, SessionHandle] = {}
        self._tasks: dict[str, A2aTask] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._rpc_seq = 0
        self._pending_rpc: dict[int, asyncio.Future] = {}
        self._turn_tasks: dict[str, asyncio.Task] = {}
        self._interaction_events: dict[str, asyncio.Event] = {}
        self._interactions: dict[str, dict[str, PendingInteraction]] = {}
        self._permission_rpc_ids: dict[str, int | str] = {}
        self._updates: dict[str, list[dict]] = {}
        self._assistant_text: dict[str, list[str]] = {}
        self._artifacts: dict[str, list[dict]] = {}
        self._stderr = bytearray()
        self._loaded_native_sessions: set[str] = set()
        self._event_queues: dict[str, asyncio.Queue[SessionEvent]] = {}

    def get_session(self, session_id: str) -> SessionHandle | None:
        return self._handles.get(session_id)

    def _workspace(self, task_id: str) -> Path:
        ws = workspace_root() / "tasks" / task_id
        (ws / "input").mkdir(parents=True, exist_ok=True)
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        return ws

    async def start(self) -> None:
        await self._ensure_connected()

    async def close(self) -> None:
        turns = list(self._turn_tasks.values())
        for turn in turns:
            turn.cancel()
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )
        await asyncio.gather(*turns, return_exceptions=True)
        self._turn_tasks.clear()
        self._loaded_native_sessions.clear()
        self._process = None

    async def _ensure_connected(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            kimi = _find_kimi()
            self._loaded_native_sessions.clear()
            self._process = await asyncio.create_subprocess_exec(
                kimi,
                "acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CI": "true"},
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            initialized = await self._rpc("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "agenthub", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            })
            if not isinstance(initialized, dict):
                raise KimiFailed("kimi ACP initialize returned invalid result")

    async def _stderr_loop(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            chunk = await self._process.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > 1_000_000:
                del self._stderr[:-1_000_000]

    async def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await self._handle_message(message)
        finally:
            error = KimiFailed("kimi ACP process closed unexpectedly")
            for future in list(self._pending_rpc.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending_rpc.clear()

    async def _handle_message(self, message: dict) -> None:
        method = message.get("method")
        if method == "session/update":
            self._handle_update(message.get("params") or {})
            return
        if method == "session/request_permission" and "id" in message:
            self._handle_permission_request(message)
            return
        if method is not None and "id" in message:
            await self._send_message({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"unsupported: {method}"},
            })
            return
        rpc_id = message.get("id")
        if isinstance(rpc_id, int):
            future = self._pending_rpc.pop(rpc_id, None)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(KimiFailed(
                    f"kimi ACP error: {message['error']}"))
            else:
                future.set_result(message.get("result"))

    def _handle_update(self, params: dict) -> None:
        session_id = self._adapter_session_id(params.get("sessionId"))
        if session_id is None:
            return
        update = params.get("update") or {}
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_thought_chunk":
            return
        if kind in {"tool_call", "tool_call_update"}:
            safe_update = {
                key: _bounded(update[key])
                for key in (
                    "sessionUpdate", "toolCallId", "title", "kind",
                    "status", "locations",
                )
                if key in update
            }
        elif kind in {
            "agent_message_chunk", "plan", "plan_update", "plan_removed",
            "current_mode_update", "config_option_update",
        }:
            safe_update = _bounded(update)
        else:
            return
        self._updates.setdefault(session_id, []).append(safe_update)
        event_type = (
            "message.delta" if kind == "agent_message_chunk"
            else "tool.updated" if kind in {"tool_call", "tool_call_update"}
            else "plan.updated"
        )
        self._emit_nowait(session_id, event_type, safe_update)
        if kind != "agent_message_chunk":
            return
        content = update.get("content") or {}
        if (isinstance(content, dict)
                and content.get("type") == "text"
                and isinstance(content.get("text"), str)):
            self._assistant_text.setdefault(session_id, []).append(
                content["text"])

    def _emit_nowait(self, session_id: str, event_type: str,
                      payload: dict[str, Any]) -> None:
        handle = self._handles.get(session_id)
        queue = self._event_queues.get(session_id)
        if handle is None or queue is None:
            return
        queue.put_nowait(SessionEvent(
            event_type=event_type, session_id=session_id,
            task_id=handle.task_id, payload=_bounded(payload),
        ))

    def _handle_permission_request(self, message: dict) -> None:
        params = message.get("params") or {}
        native_id = params.get("sessionId")
        session_id = self._adapter_session_id(native_id)
        if session_id is None:
            asyncio.create_task(self._send_message({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            }))
            return
        task = self._tasks[session_id]
        tool = params.get("toolCall") or {}
        locations = tool.get("locations") or []
        paths = [
            item.get("path") for item in locations
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        raw_input = _bounded(tool.get("rawInput"))
        interaction_id = f"I-{uuid.uuid4()}"
        interaction = PendingInteraction(
            interaction_id=interaction_id,
            kind="approval",
            session_id=session_id,
            task_id=task.id,
            native_request_id=str(message["id"]),
            native_session_id=native_id,
            payload={
                "approvalId": str(message["id"]),
                "callId": tool.get("toolCallId"),
                "toolName": tool.get("kind") or tool.get("title") or "unknown",
                "reason": tool.get("title") or "Kimi tool permission request",
                "options": _bounded(params.get("options") or []),
                "inspectable": bool(paths or raw_input),
                "toolView": {
                    "title": tool.get("title"),
                    "kind": tool.get("kind"),
                    "paths": paths,
                    "rawInput": raw_input,
                },
            },
        )
        self._interactions.setdefault(session_id, {})[interaction_id] = interaction
        self._permission_rpc_ids[interaction_id] = message["id"]
        self._interaction_events.setdefault(session_id, asyncio.Event()).set()
        self._emit_nowait(session_id, "interaction.requested", {
            "interactionId": interaction_id,
            "kind": interaction.kind,
            "nativeRequestId": interaction.native_request_id,
            "payload": interaction.payload,
        })

    def _adapter_session_id(self, native_id: str | None) -> str | None:
        if not native_id:
            return None
        for session_id, handle in self._handles.items():
            if handle.native_session_id == native_id:
                return session_id
        return None

    async def _ensure_process_stream(self) -> None:
        if (self._process is None or self._process.returncode is not None
                or self._process.stdin is None):
            raise KimiFailed("kimi ACP is not connected")

    async def _send_message(self, message: dict) -> None:
        await self._ensure_process_stream()
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            assert self._process is not None and self._process.stdin is not None
            self._process.stdin.write(raw.encode("utf-8") + b"\n")
            await self._process.stdin.drain()

    async def _rpc(self, method: str, params: dict) -> Any:
        await self._ensure_process_stream()
        self._rpc_seq += 1
        rpc_id = self._rpc_seq
        future = asyncio.get_running_loop().create_future()
        self._pending_rpc[rpc_id] = future
        await self._send_message({
            "jsonrpc": "2.0", "id": rpc_id,
            "method": method, "params": params,
        })
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending_rpc.pop(rpc_id, None)
            raise KimiTimeout(
                f"kimi ACP {method} exceeded {self.timeout_seconds}s") from exc

    async def _notify(self, method: str, params: dict) -> None:
        await self._send_message({
            "jsonrpc": "2.0", "method": method, "params": params,
        })

    async def start_session(self, task: A2aTask, *, session_id: str,
                            metadata: dict[str, Any]) -> SessionHandle:
        await self._ensure_connected()
        native = metadata.get("nativeSessionId") or task.native_session_id
        ws = self._workspace(task.id)
        params = {"cwd": str(ws), "mcpServers": []}
        if native:
            params["sessionId"] = native
            await self._rpc("session/load", params)
        else:
            created = await self._rpc("session/new", params)
            native = created.get("sessionId") if isinstance(created, dict) else None
            if not isinstance(native, str) or not native:
                raise KimiFailed("kimi ACP session/new returned no sessionId")
        await self._configure_native_session(native)
        self._loaded_native_sessions.add(native)
        handle = SessionHandle(
            session_id=session_id,
            task_id=task.id,
            native_session_id=native,
            status="active",
            context_revision=task.context_revision,
        )
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        self._interactions.setdefault(session_id, {})
        self._interaction_events.setdefault(session_id, asyncio.Event())
        self._event_queues.setdefault(session_id, asyncio.Queue())
        return handle

    async def send_message(self, session_id: str,
                           message: SessionMessage) -> SessionTurnResult:
        handle = self._handles.get(session_id)
        task = self._tasks.get(session_id)
        if handle is None or task is None:
            raise KeyError(f"session not found: {session_id}")
        if handle.status == "canceled":
            raise SessionCapabilityError("session is canceled")
        if session_id in self._turn_tasks:
            raise SessionCapabilityError("session already has an active turn")
        await self._ensure_native_loaded(session_id)
        ws = self._workspace(task.id)
        (ws / "context.md").write_text(
            f"# Task {task.id}\n\n## Turn\n\n{message.content}\n",
            encoding="utf-8",
        )
        self._interaction_events[session_id].clear()
        self._assistant_text[session_id] = []
        self._updates[session_id] = []
        prompt = message.content + "\n\n工作完成后，把结果摘要写入最后一轮回复。"
        turn = asyncio.create_task(self._rpc("session/prompt", {
            "sessionId": handle.native_session_id,
            "messageId": message.message_id,
            "prompt": [{"type": "text", "text": prompt}],
        }))
        self._turn_tasks[session_id] = turn
        self._emit_nowait(session_id, "turn.started", {
            "messageId": message.message_id,
            "contextRevision": handle.context_revision,
        })
        return await self._await_turn_or_interaction(session_id)

    async def _ensure_native_loaded(self, session_id: str) -> None:
        """Reload a durable session after the shared ACP subprocess restarts."""
        await self._ensure_connected()
        handle = self._handles[session_id]
        native = handle.native_session_id
        if not native:
            raise SessionCapabilityError("native Kimi session ID is missing")
        if native in self._loaded_native_sessions:
            return
        task = self._tasks[session_id]
        await self._rpc("session/load", {
            "cwd": str(self._workspace(task.id)),
            "mcpServers": [],
            "sessionId": native,
        })
        await self._configure_native_session(native)
        self._loaded_native_sessions.add(native)

    async def _configure_native_session(self, native_session_id: str) -> None:
        model = os.environ.get("LAS_KIMI_CLI_MODEL", "").strip()
        if model:
            await self._rpc("session/set_model", {
                "sessionId": native_session_id,
                "modelId": model,
            })

    async def _await_turn_or_interaction(
            self, session_id: str) -> SessionTurnResult:
        turn = self._turn_tasks[session_id]
        interaction_wait = asyncio.create_task(
            self._interaction_events[session_id].wait())
        try:
            done, _ = await asyncio.wait(
                {turn, interaction_wait},
                timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await self._notify("session/cancel", {
                    "sessionId": self._handles[session_id].native_session_id})
                raise KimiTimeout(
                    f"kimi ACP prompt exceeded {self.timeout_seconds}s")
            if (interaction_wait in done
                    and self.list_pending_interactions(session_id)):
                self._handles[session_id].status = "blocked"
                return SessionTurnResult(state="input-required")
            await turn
            self._turn_tasks.pop(session_id, None)
            self._handles[session_id].status = "completed"
            self._emit_nowait(session_id, "turn.completed", {})
            artifacts = self._collect_turn_artifacts(session_id)
            self._artifacts[session_id] = artifacts
            return SessionTurnResult(state="completed", artifacts=artifacts)
        finally:
            if not interaction_wait.done():
                interaction_wait.cancel()
            await asyncio.gather(interaction_wait, return_exceptions=True)

    def _collect_turn_artifacts(self, session_id: str) -> list[dict]:
        task = self._tasks[session_id]
        ws = self._workspace(task.id)
        wire = "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in self._updates.get(session_id, [])
        ).encode("utf-8")
        summary = "".join(self._assistant_text.get(session_id, [])).strip()
        artifacts = [
            save_artifact(task.id, "kimi-acp.jsonl", wire, "log"),
            save_artifact(task.id, "kimi-stderr.log", bytes(self._stderr), "log"),
            save_artifact(
                task.id,
                "last-message.md",
                (summary or "（无 assistant 文本输出，详见 kimi-acp.jsonl）").encode(),
                "report",
            ),
        ]
        artifacts.extend(self._workspace_artifacts(task.id, ws))
        return artifacts

    def _workspace_artifacts(self, task_id: str, ws: Path) -> list[dict]:
        out = []
        for path in sorted(ws.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ws)
            if (rel.parts[0] in {"logs", "input", "artifacts"}
                    or rel.name == "context.md"):
                continue
            out.append(save_artifact(
                task_id, f"workspace/{rel}", path.read_bytes(), "file"))
        return out

    def list_pending_interactions(
            self, session_id: str) -> list[PendingInteraction]:
        return [
            item for item in self._interactions.get(session_id, {}).values()
            if item.status == "pending"
        ]

    async def stream_events(
            self, session_id: str) -> AsyncIterator[SessionEvent]:
        if session_id not in self._event_queues:
            raise KeyError(f"session not found: {session_id}")
        queue = self._event_queues[session_id]
        while True:
            yield await queue.get()

    async def respond_interaction(
        self,
        session_id: str,
        interaction_id: str,
        response: dict[str, Any],
        *,
        responded_by: str,
    ) -> SessionTurnResult:
        interaction = self._interactions.get(session_id, {}).get(interaction_id)
        if interaction is None or interaction.status != "pending":
            raise KeyError(f"pending interaction not found: {interaction_id}")
        if responded_by not in {"user", "hermes"}:
            raise PermissionError("only user or hermes may respond")
        outcome = response.get("outcome")
        if outcome not in {"allowed-once", "rejected"}:
            raise ValueError("outcome must be allowed-once or rejected")
        options = interaction.payload.get("options") or []
        wanted_kind = "allow_once" if outcome == "allowed-once" else "reject_once"
        option = next(
            (item for item in options
             if isinstance(item, dict) and item.get("kind") == wanted_kind),
            None,
        )
        if option is None:
            raise SessionCapabilityError(
                f"Kimi did not offer permission option: {wanted_kind}")
        if outcome == "allowed-once":
            authorization = response.get("authorization")
            if not isinstance(authorization, dict):
                raise PermissionError(
                    "allowed-once requires an approved ActionIntent receipt")
            expected = {
                "taskId": interaction.task_id,
                "interactionId": interaction.interaction_id,
                "nativeRequestId": interaction.native_request_id,
                "nativeSessionId": interaction.native_session_id,
                "contextRevision": self._handles[session_id].context_revision,
                "status": "approved",
            }
            if any(authorization.get(key) != value
                   for key, value in expected.items()):
                raise PermissionError(
                    "ActionIntent receipt does not match this interaction")
            if not verify_action_receipt(authorization):
                raise PermissionError("ActionIntent receipt signature is invalid")
        rpc_id = self._permission_rpc_ids.pop(interaction_id)
        await self._send_message({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "outcome": {
                    "outcome": "selected",
                    "optionId": option["optionId"],
                }
            },
        })
        interaction.status = "responded"
        interaction.responded_by = responded_by
        interaction.response = _bounded(response)
        self._interaction_events[session_id].clear()
        self._handles[session_id].status = "active"
        self._emit_nowait(session_id, "interaction.responded", {
            "interactionId": interaction_id, "outcome": outcome,
            "respondedBy": responded_by,
        })
        return SessionTurnResult(state="working")

    async def continue_after_interaction(
            self, session_id: str) -> SessionTurnResult:
        if session_id not in self._turn_tasks:
            raise SessionCapabilityError("session has no suspended turn")
        return await self._await_turn_or_interaction(session_id)

    async def resume_session(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native Kimi session ID is missing")
        handle.status = "active"
        return handle

    async def interrupt(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        await self._notify("session/cancel", {
            "sessionId": handle.native_session_id})
        handle.status = "paused"
        return handle

    async def cancel(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        await self._notify("session/cancel", {
            "sessionId": handle.native_session_id})
        handle.status = "canceled"
        turn = self._turn_tasks.pop(session_id, None)
        if turn and not turn.done():
            turn.cancel()
        return handle

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._artifacts.get(session_id, []))
