"""Native resumable Codex CLI Session Adapter (Codex CLI >= 0.148)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from adapters.codex.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    CodexFailed,
    CodexNotAvailable,
    CodexTimeout,
)
from adapters.common import A2aTask, save_artifact, workspace_root
from adapters.session import (
    SessionAdapter,
    SessionCapabilities,
    SessionCapabilityError,
    SessionHandle,
    SessionMessage,
    SessionTurnResult,
)


def extract_codex_session_id(jsonl: str) -> str | None:
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


class CodexSessionAdapter(SessionAdapter):
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
        native = metadata.get("nativeSessionId") or task.native_session_id
        handle = SessionHandle(
            session_id=session_id, task_id=task.id,
            native_session_id=native, status="active",
            context_revision=task.context_revision)
        self._handles[session_id] = handle
        self._tasks[session_id] = task
        return handle

    def _workspace(self, task_id: str) -> Path:
        ws = workspace_root() / "tasks" / task_id
        (ws / "input").mkdir(parents=True, exist_ok=True)
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        return ws

    def _command(self, codex: str, ws: Path, last_message: Path,
                 prompt: str, native_session_id: str | None) -> list[str]:
        if native_session_id:
            return [
                codex, "exec", "resume", "--skip-git-repo-check", "--json",
                "-o", str(last_message), native_session_id, prompt,
            ]
        return [
            codex, "exec", "--sandbox", "workspace-write",
            "--skip-git-repo-check", "-C", str(ws), "--json",
            "-o", str(last_message), prompt,
        ]

    async def send_message(self, session_id: str,
                           message: SessionMessage) -> SessionTurnResult:
        handle = self._handles.get(session_id)
        task = self._tasks.get(session_id)
        if handle is None or task is None:
            raise KeyError(f"session not found: {session_id}")
        if handle.status == "canceled":
            raise SessionCapabilityError("session is canceled")
        codex = shutil.which("codex")
        if not codex:
            raise CodexNotAvailable("codex CLI not found in PATH")
        ws = self._workspace(task.id)
        (ws / "context.md").write_text(
            f"# Task {task.id}\n\n## Turn\n\n{message.content}\n",
            encoding="utf-8")
        last_message = ws / "logs" / "last-message.md"
        command = self._command(
            codex, ws, last_message,
            message.content + "\n\n工作完成后，把结果摘要写入最后一轮回复。",
            handle.native_session_id)
        env = dict(os.environ)
        env.setdefault("CI", "true")
        if "CLIPROXY_API_KEY" not in env:
            from common import config as cfg

            key = cfg.llm_api_key()
            if key:
                env["CLIPROXY_API_KEY"] = key
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(ws), env=env)
        self._processes[session_id] = proc

        async def collect_output() -> bytes:
            chunks: list[bytes] = []
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
                discovered = extract_codex_session_id(
                    line.decode("utf-8", errors="replace"))
                if discovered:
                    handle.native_session_id = discovered
            await proc.wait()
            return b"".join(chunks)

        try:
            stdout = await asyncio.wait_for(
                collect_output(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise CodexTimeout(
                f"codex exec exceeded {self.timeout_seconds}s")
        finally:
            self._processes.pop(session_id, None)

        output = (stdout or b"").decode("utf-8", errors="replace")
        discovered = extract_codex_session_id(output)
        if discovered:
            handle.native_session_id = discovered
        artifacts = [save_artifact(
            task.id, "codex.jsonl", stdout or b"", "log")]
        if last_message.exists():
            artifacts.append(save_artifact(
                task.id, "last-message.md", last_message.read_bytes(),
                "report"))
        artifacts.extend(self._workspace_artifacts(task.id, ws))
        self._artifacts[session_id] = artifacts
        if proc.returncode != 0:
            raise CodexFailed(
                f"codex exec exited {proc.returncode}; see codex.jsonl")
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
            raise SessionCapabilityError("native Codex session ID is missing")
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
