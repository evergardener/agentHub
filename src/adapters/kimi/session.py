"""Native resumable Kimi Code CLI Session Adapter (Kimi Code >= 0.37)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from adapters.common import A2aTask, save_artifact, workspace_root
from adapters.kimi.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    KimiFailed,
    KimiTimeout,
    _extract_assistant_text,
    _find_kimi,
)
from adapters.session import (
    SessionAdapter,
    SessionCapabilities,
    SessionCapabilityError,
    SessionHandle,
    SessionMessage,
    SessionTurnResult,
)


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
    """Fallback to Kimi's durable index when stream-json omits sessionId."""
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


class KimiSessionAdapter(SessionAdapter):
    capabilities = SessionCapabilities(
        multi_turn=True, resume=True, native_resume=True,
        durable_session=True, streaming=False, pause=False,
        interrupt=True, cancel=True)

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds
        self._handles: dict[str, SessionHandle] = {}
        self._tasks: dict[str, A2aTask] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._artifacts: dict[str, list[dict]] = {}

    def get_session(self, session_id: str) -> SessionHandle | None:
        return self._handles.get(session_id)

    async def start_session(self, task: A2aTask, *, session_id: str,
                            metadata: dict[str, Any]) -> SessionHandle:
        handle = SessionHandle(
            session_id=session_id, task_id=task.id,
            native_session_id=(
                metadata.get("nativeSessionId") or task.native_session_id),
            status="active", context_revision=task.context_revision)
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        return handle

    def _workspace(self, task_id: str) -> Path:
        ws = workspace_root() / "tasks" / task_id
        (ws / "input").mkdir(parents=True, exist_ok=True)
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        return ws

    def _command(self, kimi: str, prompt: str,
                 native_session_id: str | None) -> list[str]:
        cmd = [kimi]
        if native_session_id:
            cmd.extend(["-S", native_session_id])
        model = os.environ.get("LAS_KIMI_CLI_MODEL", "").strip()
        if model:
            cmd.extend(["-m", model])
        cmd.extend(["--output-format=stream-json", "-p", prompt])
        return cmd

    async def send_message(self, session_id: str,
                           message: SessionMessage) -> SessionTurnResult:
        handle = self._handles.get(session_id)
        task = self._tasks.get(session_id)
        if handle is None or task is None:
            raise KeyError(f"session not found: {session_id}")
        if handle.status == "canceled":
            raise SessionCapabilityError("session is canceled")
        kimi = _find_kimi()
        ws = self._workspace(task.id)
        prior_workspace_session = find_kimi_session_for_workspace(ws)
        (ws / "context.md").write_text(
            f"# Task {task.id}\n\n## Turn\n\n{message.content}\n",
            encoding="utf-8")
        prompt = message.content + "\n\n工作完成后，把结果摘要写入最后一轮回复。"
        proc = await asyncio.create_subprocess_exec(
            *self._command(kimi, prompt, handle.native_session_id),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(ws), env={**os.environ, "CI": "true"})
        self._processes[session_id] = proc

        async def read_stdout() -> bytes:
            chunks: list[bytes] = []
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
                discovered = extract_kimi_session_id(
                    line.decode("utf-8", errors="replace"))
                if discovered:
                    handle.native_session_id = discovered
            return b"".join(chunks)

        async def watch_index() -> None:
            while proc.returncode is None and not handle.native_session_id:
                discovered = find_kimi_session_for_workspace(ws)
                if discovered and discovered != prior_workspace_session:
                    handle.native_session_id = discovered
                    return
                await asyncio.sleep(0.1)

        async def collect_output() -> tuple[bytes, bytes]:
            stdout_task = asyncio.create_task(read_stdout())
            assert proc.stderr is not None
            stderr_task = asyncio.create_task(proc.stderr.read())
            index_task = asyncio.create_task(watch_index())
            tasks = (stdout_task, stderr_task, index_task)
            try:
                await proc.wait()
                return await stdout_task, await stderr_task
            finally:
                for pending in tasks:
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                collect_output(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise KimiTimeout(f"kimi -p exceeded {self.timeout_seconds}s")
        finally:
            self._processes.pop(session_id, None)

        output = (stdout or b"").decode("utf-8", errors="replace")
        discovered = extract_kimi_session_id(output)
        if not discovered:
            indexed = find_kimi_session_for_workspace(ws)
            if indexed != prior_workspace_session:
                discovered = indexed
        if discovered:
            handle.native_session_id = discovered
        artifacts = [
            save_artifact(task.id, "kimi.jsonl", stdout or b"", "log"),
            save_artifact(task.id, "kimi-stderr.log", stderr or b"", "log"),
        ]
        summary = _extract_assistant_text(output)
        artifacts.append(save_artifact(
            task.id, "last-message.md",
            (summary or "（无 assistant 文本输出，详见 kimi.jsonl）").encode(),
            "report"))
        artifacts.extend(self._workspace_artifacts(task.id, ws))
        self._artifacts[session_id] = artifacts
        if proc.returncode != 0:
            raise KimiFailed(
                f"kimi -p exited {proc.returncode}; see kimi.jsonl")
        handle.status = "completed"
        return SessionTurnResult(state="completed", artifacts=artifacts)

    def _workspace_artifacts(self, task_id: str, ws: Path) -> list[dict]:
        out = []
        for path in sorted(ws.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ws)
            if rel.parts[0] in {"logs", "input", "artifacts"} or \
                    rel.name == "context.md":
                continue
            out.append(save_artifact(
                task_id, f"workspace/{rel}", path.read_bytes(), "file"))
        return out

    async def resume_session(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        if not handle.native_session_id:
            raise SessionCapabilityError("native Kimi session ID is missing")
        handle.status = "active"
        return handle

    async def interrupt(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            proc.terminate()
        handle.status = "paused"
        return handle

    async def cancel(self, session_id: str) -> SessionHandle:
        handle = self._handles[session_id]
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            proc.kill()
        handle.status = "canceled"
        return handle

    async def collect_artifacts(self, session_id: str) -> list[dict]:
        return list(self._artifacts.get(session_id, []))
