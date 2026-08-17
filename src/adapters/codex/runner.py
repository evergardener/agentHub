"""Codex 运行时 — 设计文档 §Phase 2：Adapter 调用本地 Codex。

执行方式：
  codex exec --sandbox workspace-write --skip-git-repo-check \
      -C <task_workspace> -o <last_message> <prompt>

权限边界（§13 Codex）：
  - filesystem/shell 限制在任务工作区（workspace-write 沙箱）
  - ssh denied by default（codex exec 非交互 + 沙箱兜底）
  - 超时看门狗：task.timeout_seconds（§17.3）
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from adapters.common import A2aTask, save_artifact, workspace_root

DEFAULT_TIMEOUT_SECONDS = 1800


class CodexNotAvailable(RuntimeError):
    pass


class CodexTimeout(RuntimeError):
    pass


class CodexFailed(RuntimeError):
    pass


def _task_workspace(task_id: str) -> Path:
    ws = workspace_root() / "tasks" / task_id
    (ws / "input").mkdir(parents=True, exist_ok=True)
    (ws / "logs").mkdir(parents=True, exist_ok=True)
    return ws


async def run(task: A2aTask, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict]:
    codex = shutil.which("codex")
    if not codex:
        raise CodexNotAvailable("codex CLI not found in PATH")

    ws = _task_workspace(task.id)
    context_file = ws / "context.md"
    context_file.write_text(
        f"# Task {task.id}\n\n## Objective\n\n{task.objective}\n",
        encoding="utf-8",
    )
    last_message = ws / "logs" / "last-message.md"
    prompt = (
        f"{task.objective}\n\n"
        "工作完成后，把结果摘要写入最后一轮回复。"
    )

    cmd = [
        codex, "exec",
        "--sandbox", "workspace-write",
        "--skip-git-repo-check",
        "-C", str(ws),
        "-o", str(last_message),
        prompt,
    ]

    env = dict(os.environ)
    env.setdefault("CI", "true")
    # codex CLI 的 config.toml 以 CLIPROXY_API_KEY 为 env_key；
    # 系统统一配置是 LAS_LLM_API_KEY（common.config），这里做映射注入。
    if "CLIPROXY_API_KEY" not in env:
        from common import config as cfg

        key = cfg.llm_api_key()
        if key:
            env["CLIPROXY_API_KEY"] = key

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,  # 防止 codex 等待 stdin 追加输入
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(ws),
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CodexTimeout(f"codex exec exceeded {timeout_seconds}s")

    artifacts: list[dict] = []
    artifacts.append(save_artifact(task.id, "codex.log", stdout or b"", "log"))
    if last_message.exists():
        artifacts.append(
            save_artifact(task.id, "last-message.md",
                          last_message.read_bytes(), "report")
        )

    if proc.returncode != 0:
        raise CodexFailed(
            f"codex exec exited {proc.returncode}; see codex.log artifact"
        )

    # 收集工作区内 Codex 产出的文件（排除 logs/input/context）
    for path in sorted(ws.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ws)
        if rel.parts[0] in ("logs", "input", "artifacts") or rel.name == "context.md":
            continue
        artifacts.append(
            save_artifact(task.id, f"workspace/{rel}", path.read_bytes(), "file")
        )
    return artifacts
