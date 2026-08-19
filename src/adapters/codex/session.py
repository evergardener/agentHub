"""Codex App Server adapter with durable threads and native approvals.

``codex exec --json`` reports tool activity only after the fact and cannot keep
an approval callback suspended for AgentHub. App Server exposes that boundary:
every thread starts read-only and only the exact authorized request is resumed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from adapters.codex.runner import (
    DEFAULT_TIMEOUT_SECONDS, CodexFailed, CodexNotAvailable, CodexTimeout,
)
from adapters.common import A2aTask, save_artifact, workspace_root
from adapters.session import (
    PendingInteraction, SessionAdapter, SessionCapabilities,
    SessionCapabilityError, SessionEvent, SessionHandle, SessionMessage,
    SessionTurnResult,
)
from common.action_receipt import verify_action_receipt


def extract_codex_session_id(jsonl: str) -> str | None:
    """Compatibility parser for previously persisted ``codex exec`` logs."""
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if event.get("type") == "thread.started":
            value = event.get("thread_id") or event.get("threadId")
            if isinstance(value, str) and value:
                return value
    return None


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key)", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)"
    r"([^\s,;]+)")


def _bounded(value: Any, *, limit: int = 4096) -> Any:
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1[REDACTED]", value[:limit])
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


class CodexSessionAdapter(SessionAdapter):
    capabilities = SessionCapabilities(
        multi_turn=True, resume=True, native_resume=True,
        durable_session=True, streaming=True, pause=False,
        interrupt=True, cancel=True, interactions=True,
    )
    _APPROVAL_METHODS = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self._handles: dict[str, SessionHandle] = {}
        self._tasks: dict[str, A2aTask] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._rpc_seq = 0
        self._pending_rpc: dict[int, asyncio.Future] = {}
        self._loaded_threads: set[str] = set()
        self._turn_futures: dict[str, asyncio.Future] = {}
        self._active_turn_ids: dict[str, str] = {}
        self._early_completions: dict[str, dict] = {}
        self._interactions: dict[str, dict[str, PendingInteraction]] = {}
        self._interaction_events: dict[str, asyncio.Event] = {}
        self._approval_rpc_ids: dict[str, int | str] = {}
        self._approval_methods: dict[str, str] = {}
        self._approval_params: dict[str, dict] = {}
        self._items: dict[tuple[str, str], dict] = {}
        self._updates: dict[str, list[dict]] = {}
        self._assistant_text: dict[str, list[str]] = {}
        self._artifacts: dict[str, list[dict]] = {}
        self._stderr = bytearray()
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
        self._fail_pending(CodexFailed("Codex App Server closed"))
        self._process = None
        self._loaded_threads.clear()

    async def _ensure_connected(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            codex = shutil.which("codex")
            if not codex:
                raise CodexNotAvailable("codex CLI not found in PATH")
            env = dict(os.environ)
            env.setdefault("CI", "true")
            if "CLIPROXY_API_KEY" not in env:
                from common import config as cfg

                key = cfg.llm_api_key()
                if key:
                    env["CLIPROXY_API_KEY"] = key
            self._loaded_threads.clear()
            self._process = await asyncio.create_subprocess_exec(
                codex, "app-server", "--stdio",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            initialized = await self._rpc("initialize", {
                "clientInfo": {
                    "name": "agenthub", "title": "AgentHub Codex Adapter",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            })
            if not isinstance(initialized, dict):
                raise CodexFailed(
                    "Codex App Server initialize returned invalid result")
            await self._notify("initialized", {})

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
            self._fail_pending(
                CodexFailed("Codex App Server process closed unexpectedly"))

    def _fail_pending(self, error: Exception) -> None:
        for future in [
            *self._pending_rpc.values(), *self._turn_futures.values()
        ]:
            if not future.done():
                future.set_exception(error)
        self._pending_rpc.clear()
        self._turn_futures.clear()

    async def _handle_message(self, message: dict) -> None:
        method = message.get("method")
        if method in self._APPROVAL_METHODS and "id" in message:
            await self._handle_approval_request(message)
            return
        if method is not None and "id" in message:
            await self._send_message({
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601,
                          "message": f"unsupported server request: {method}"},
            })
            return
        if method is not None:
            self._handle_notification(method, message.get("params") or {})
            return
        rpc_id = message.get("id")
        if isinstance(rpc_id, int):
            future = self._pending_rpc.pop(rpc_id, None)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(CodexFailed(
                    f"Codex App Server error: {message['error']}"))
            else:
                future.set_result(message.get("result"))

    def _handle_notification(self, method: str, params: dict) -> None:
        native_id = params.get("threadId")
        session_id = self._adapter_session_id(native_id)
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str):
                future = self._turn_futures.get(turn_id)
                if future is not None and not future.done():
                    future.set_result(turn)
                else:
                    self._early_completions[turn_id] = turn
        if session_id is None:
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                self._assistant_text.setdefault(session_id, []).append(delta)
                self._updates.setdefault(session_id, []).append(_bounded({
                    "method": method, "turnId": params.get("turnId"),
                    "itemId": params.get("itemId"), "delta": delta,
                }))
                self._emit_nowait(session_id, "message.delta", {
                    "turnId": params.get("turnId"),
                    "itemId": params.get("itemId"), "delta": delta,
                })
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            item_id = item.get("id")
            if isinstance(item_id, str):
                self._items[(native_id, item_id)] = item
            safe_item = self._safe_item(item)
            if safe_item:
                self._updates.setdefault(session_id, []).append({
                    "method": method, "turnId": params.get("turnId"),
                    "item": safe_item,
                })
                self._emit_nowait(session_id, "item.lifecycle", {
                    "phase": "started" if method == "item/started"
                    else "completed",
                    "turnId": params.get("turnId"), "item": safe_item,
                })
            return
        if method in {"turn/started", "turn/completed", "turn/diff/updated",
                      "turn/plan/updated"}:
            safe = {"method": method}
            for key in ("threadId", "turnId", "diff", "plan"):
                if key in params:
                    safe[key] = _bounded(params[key])
            if method == "turn/completed":
                turn = params.get("turn") or {}
                safe["turn"] = _bounded({
                    "id": turn.get("id"), "status": turn.get("status"),
                    "error": turn.get("error"),
                })
            self._updates.setdefault(session_id, []).append(safe)
            self._emit_nowait(session_id, method.replace("/", "."), safe)

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

    @staticmethod
    def _safe_item(item: dict) -> dict:
        kind = item.get("type")
        if kind == "commandExecution":
            return _bounded({
                key: item.get(key)
                for key in ("id", "type", "command", "commandActions",
                            "cwd", "status", "exitCode", "durationMs")
                if key in item
            })
        if kind == "fileChange":
            return _bounded({
                key: item.get(key)
                for key in ("id", "type", "changes", "status") if key in item
            })
        if kind == "agentMessage":
            return _bounded({
                key: item.get(key)
                for key in ("id", "type", "text", "phase") if key in item
            })
        return {}

    async def _handle_approval_request(self, message: dict) -> None:
        params = message.get("params") or {}
        method = message["method"]
        native_id = params.get("threadId")
        session_id = self._adapter_session_id(native_id)
        if session_id is None:
            await self._send_message({
                "jsonrpc": "2.0", "id": message["id"],
                "result": self._approval_result(method, params, False),
            })
            return
        task = self._tasks[session_id]
        item_id = params.get("itemId")
        item = self._items.get((native_id, item_id), {})
        paths = self._approval_paths(params, item)
        tool_name = {
            "item/commandExecution/requestApproval": "shell",
            "item/fileChange/requestApproval": "edit",
            "item/permissions/requestApproval": "permissions",
        }[method]
        command = params.get("command") or item.get("command")
        interaction_id = f"I-{uuid.uuid4()}"
        interaction = PendingInteraction(
            interaction_id=interaction_id, kind="approval",
            session_id=session_id, task_id=task.id,
            native_request_id=str(message["id"]), native_session_id=native_id,
            payload={
                "approvalId": params.get("approvalId") or str(message["id"]),
                "callId": item_id, "toolName": tool_name,
                "reason": params.get("reason") or f"Codex {tool_name} request",
                "inspectable": bool(paths or command or params.get("permissions")),
                "toolView": _bounded({
                    "title": f"Codex {tool_name}", "kind": tool_name,
                    "paths": paths, "cwd": params.get("cwd") or item.get("cwd"),
                    "command": command,
                    "commandActions": params.get("commandActions")
                    or item.get("commandActions"),
                    "permissions": params.get("permissions"),
                }),
            },
        )
        self._interactions.setdefault(session_id, {})[interaction_id] = interaction
        self._approval_rpc_ids[interaction_id] = message["id"]
        self._approval_methods[interaction_id] = method
        self._approval_params[interaction_id] = params
        self._interaction_events.setdefault(session_id, asyncio.Event()).set()
        self._emit_nowait(session_id, "interaction.requested", {
            "interactionId": interaction_id,
            "kind": interaction.kind,
            "nativeRequestId": interaction.native_request_id,
            "payload": interaction.payload,
        })

    @staticmethod
    def _approval_paths(params: dict, item: dict) -> list[str]:
        found: list[str] = []

        def visit(value: Any, key: str | None = None) -> None:
            if len(found) >= 50:
                return
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, child_key)
            elif isinstance(value, list):
                for child in value:
                    visit(child, key)
            elif (isinstance(value, str)
                  and key in {"path", "read", "write", "grantRoot"}
                  and value not in found):
                found.append(value)

        visit(params.get("commandActions"))
        visit(params.get("permissions"))
        visit(params.get("grantRoot"), "grantRoot")
        visit(item.get("changes"))
        return found

    @staticmethod
    def _approval_result(method: str, params: dict, allowed: bool) -> dict:
        if method == "item/permissions/requestApproval":
            return {
                "permissions": params.get("permissions") if allowed else {},
                "scope": "turn", "strictAutoReview": True,
            }
        return {"decision": "accept" if allowed else "decline"}

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
            raise CodexFailed("Codex App Server is not connected")

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
            "jsonrpc": "2.0", "id": rpc_id, "method": method,
            "params": params,
        })
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending_rpc.pop(rpc_id, None)
            raise CodexTimeout(
                f"Codex App Server {method} exceeded "
                f"{self.timeout_seconds}s") from exc

    async def _notify(self, method: str, params: dict) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        await self._send_message(message)

    def _thread_params(self, ws: Path) -> dict:
        params: dict[str, Any] = {
            "cwd": str(ws), "runtimeWorkspaceRoots": [str(ws)],
            "sandbox": "read-only", "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        }
        model = os.environ.get("LAS_CODEX_CLI_MODEL", "").strip()
        if model:
            params["model"] = model
        return params

    async def start_session(self, task: A2aTask, *, session_id: str,
                            metadata: dict[str, Any]) -> SessionHandle:
        await self._ensure_connected()
        native = metadata.get("nativeSessionId") or task.native_session_id
        ws = self._workspace(task.id)
        if native:
            await self._rpc("thread/resume", {
                "threadId": native, **self._thread_params(ws),
            })
        else:
            result = await self._rpc("thread/start", {
                **self._thread_params(ws), "ephemeral": False,
            })
            thread = result.get("thread") if isinstance(result, dict) else None
            native = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(native, str) or not native:
            raise CodexFailed("Codex App Server returned no thread id")
        self._loaded_threads.add(native)
        handle = SessionHandle(
            session_id=session_id, task_id=task.id,
            native_session_id=native, status="active",
            context_revision=task.context_revision,
        )
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        self._interactions.setdefault(session_id, {})
        self._interaction_events.setdefault(session_id, asyncio.Event())
        self._event_queues.setdefault(session_id, asyncio.Queue())
        return handle

    async def _ensure_native_loaded(self, session_id: str) -> None:
        await self._ensure_connected()
        handle = self._handles[session_id]
        native = handle.native_session_id
        if not native:
            raise SessionCapabilityError("native Codex thread ID is missing")
        if native in self._loaded_threads:
            return
        task = self._tasks[session_id]
        await self._rpc("thread/resume", {
            "threadId": native,
            **self._thread_params(self._workspace(task.id)),
        })
        self._loaded_threads.add(native)

    async def send_message(self, session_id: str,
                           message: SessionMessage) -> SessionTurnResult:
        handle = self._handles.get(session_id)
        task = self._tasks.get(session_id)
        if handle is None or task is None:
            raise KeyError(f"session not found: {session_id}")
        if handle.status == "canceled":
            raise SessionCapabilityError("session is canceled")
        if session_id in self._active_turn_ids:
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
        result = await self._rpc("turn/start", {
            "threadId": handle.native_session_id,
            "input": [{"type": "text", "text": prompt}],
            "clientUserMessageId": message.message_id,
            "approvalPolicy": "on-request", "approvalsReviewer": "user",
        })
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexFailed("Codex App Server turn/start returned no turn id")
        future = asyncio.get_running_loop().create_future()
        early = self._early_completions.pop(turn_id, None)
        if early is not None:
            future.set_result(early)
        self._turn_futures[turn_id] = future
        self._active_turn_ids[session_id] = turn_id
        return await self._await_turn_or_interaction(session_id)

    async def _await_turn_or_interaction(
            self, session_id: str) -> SessionTurnResult:
        turn_id = self._active_turn_ids[session_id]
        turn = self._turn_futures[turn_id]
        interaction_wait = asyncio.create_task(
            self._interaction_events[session_id].wait())
        try:
            done, _ = await asyncio.wait(
                {turn, interaction_wait}, timeout=self.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await self._interrupt_native(session_id)
                raise CodexTimeout(
                    f"Codex App Server turn exceeded {self.timeout_seconds}s")
            if (interaction_wait in done
                    and self.list_pending_interactions(session_id)):
                self._handles[session_id].status = "blocked"
                return SessionTurnResult(state="input-required")
            completed = await turn
            self._turn_futures.pop(turn_id, None)
            self._active_turn_ids.pop(session_id, None)
            status = completed.get("status")
            if status != "completed":
                raise CodexFailed(
                    f"Codex turn ended with status {status}: "
                    f"{_bounded(completed.get('error'))}")
            self._handles[session_id].status = "completed"
            artifacts = self._collect_turn_artifacts(session_id)
            self._artifacts[session_id] = artifacts
            return SessionTurnResult(state="completed", artifacts=artifacts)
        except BaseException:
            # A process failure/timeout must not leave a phantom active turn
            # that prevents an explicit retry or durable thread reload.
            self._turn_futures.pop(turn_id, None)
            self._active_turn_ids.pop(session_id, None)
            raise
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
            save_artifact(task.id, "codex-app-server.jsonl", wire, "log"),
            save_artifact(
                task.id, "last-message.md",
                (summary or "（无 assistant 文本输出，详见 codex-app-server.jsonl）")
                .encode(), "report",
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
        self, session_id: str, interaction_id: str, response: dict[str, Any],
        *, responded_by: str,
    ) -> SessionTurnResult:
        interaction = self._interactions.get(session_id, {}).get(interaction_id)
        if interaction is None or interaction.status != "pending":
            raise KeyError(f"pending interaction not found: {interaction_id}")
        if responded_by not in {"user", "hermes"}:
            raise PermissionError("only user or hermes may respond")
        outcome = response.get("outcome")
        if outcome not in {"allowed-once", "rejected"}:
            raise ValueError("outcome must be allowed-once or rejected")
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
            offered = self._approval_params[interaction_id].get(
                "availableDecisions")
            if (isinstance(offered, list) and offered
                    and "accept" not in offered):
                raise SessionCapabilityError(
                    "Codex did not offer a one-shot accept decision")
        method = self._approval_methods.pop(interaction_id)
        params = self._approval_params.pop(interaction_id)
        rpc_id = self._approval_rpc_ids.pop(interaction_id)
        await self._send_message({
            "jsonrpc": "2.0", "id": rpc_id,
            "result": self._approval_result(
                method, params, outcome == "allowed-once"),
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
        if session_id not in self._active_turn_ids:
            raise SessionCapabilityError("session has no suspended turn")
        return await self._await_turn_or_interaction(session_id)

    async def resume_session(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native Codex thread ID is missing")
        await self._ensure_native_loaded(session_id)
        handle.status = "active"
        return handle

    async def _interrupt_native(self, session_id: str) -> None:
        turn_id = self._active_turn_ids.get(session_id)
        if not turn_id:
            return
        await self._rpc("turn/interrupt", {
            "threadId": self._handles[session_id].native_session_id,
            "turnId": turn_id,
        })

    async def interrupt(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        await self._interrupt_native(session_id)
        handle.status = "paused"
        return handle

    async def cancel(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        await self._interrupt_native(session_id)
        handle.status = "canceled"
        return handle

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._artifacts.get(session_id, []))
