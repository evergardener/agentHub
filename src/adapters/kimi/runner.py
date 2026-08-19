"""Legacy one-shot Kimi prompt runner.

The production Kimi A2A server uses ``KimiSessionAdapter`` over ``kimi acp``.
This module remains only for explicit compatibility callers and parsing old
stream-json artifacts; prompt mode has no AgentHub-addressable approval
callback and must not be used as the production modification boundary.

执行方式（对齐 codex runner 的形态）：
  kimi -p --output-format stream-json <prompt>     # cwd = 任务工作区

设计约束：
  - CLI 为官方单二进制（默认 ~/.kimi-code/bin/kimi，install.sh 安装）。
    找不到时显式抛 KimiNotAvailable——**不回退到 HTTP 模型调用**，
    本 worker 必须是真的本地 kimi（2026-08-18 起，替代原 cliproxy
    HTTP runner；原 HTTP 路径是 codex 额度用尽时的临时替身）。
  - 认证：`kimi login`（OAuth device-code）或 Moonshot API key，
    一次性交互配置，token 持久化于 ~/.kimi-code，launchd 常驻可用。
  - ``-p`` 无头模式不能把原生逐工具审批桥接回控制面。生产服务禁止
    依赖本 runner 执行修改任务；ACP Adapter 才能保持 tool call 挂起并
    接收 ActionIntent receipt。
  - 可选 LAS_KIMI_CLI_MODEL 指定模型别名（`kimi -m`），缺省用 CLI
    配置的 default_model。
  - 超时看门狗：task 级 timeout（§17.3），默认 1800s。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from adapters.common import A2aTask, save_artifact, workspace_root

DEFAULT_TIMEOUT_SECONDS = 1800
_BUNDLED_CLI = Path.home() / ".kimi-code" / "bin" / "kimi"


class KimiNotAvailable(RuntimeError):
    pass


class KimiTimeout(RuntimeError):
    pass


class KimiFailed(RuntimeError):
    pass


def _find_kimi() -> str:
    """定位 kimi CLI：PATH 优先，其次官方安装的固定路径。"""
    found = shutil.which("kimi")
    if found:
        return found
    if _BUNDLED_CLI.exists():
        return str(_BUNDLED_CLI)
    raise KimiNotAvailable(
        "kimi CLI 未安装（curl -fsSL https://code.kimi.com/kimi-code/"
        "install.sh | bash），kimi worker 拒绝回退到 HTTP 模型调用")


def _extract_assistant_text(jsonl: str) -> str:
    """从 stream-json 输出中提取 assistant 文本（容忍字段形状差异）。"""
    parts: list[str] = []

    def _text_from_content(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text"))
        return ""

    for line in jsonl.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role") or obj.get("type")
        if role != "assistant":
            continue
        text = _text_from_content(obj.get("content"))
        if not text:
            msg = obj.get("message")
            if isinstance(msg, dict):
                text = _text_from_content(msg.get("content"))
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def _task_workspace(task_id: str) -> Path:
    ws = workspace_root() / "tasks" / task_id
    (ws / "input").mkdir(parents=True, exist_ok=True)
    (ws / "logs").mkdir(parents=True, exist_ok=True)
    return ws


async def run(task: A2aTask,
              timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict]:
    kimi = _find_kimi()

    ws = _task_workspace(task.id)
    (ws / "context.md").write_text(
        f"# Task {task.id}\n\n## Objective\n\n{task.objective}\n",
        encoding="utf-8",
    )
    prompt = (f"{task.objective}\n\n"
              "工作完成后，把结果摘要写入最后一轮回复。")

    # 注意两个 0.37.1 实测怪癖：--output-format 必须 = 形式；且必须放在
    # -p 之前（-p 会把紧随其后的任何 token 吞为 prompt 值）
    cmd = [kimi, "--output-format=stream-json", "-p", prompt]
    model = os.environ.get("LAS_KIMI_CLI_MODEL", "").strip()
    if model:
        cmd[1:1] = ["-m", model]

    env = dict(os.environ)
    env.setdefault("CI", "true")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ws),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise KimiTimeout(f"kimi -p exceeded {timeout_seconds}s")

    out_text = (stdout or b"").decode("utf-8", errors="replace")
    artifacts: list[dict] = [
        save_artifact(task.id, "kimi.jsonl", stdout or b"", "log"),
        save_artifact(task.id, "kimi-stderr.log", stderr or b"", "log"),
    ]

    if proc.returncode != 0:
        raise KimiFailed(
            f"kimi -p exited {proc.returncode}; see kimi.jsonl / "
            f"kimi-stderr.log artifacts: {out_text[-300:]!r}")

    summary = _extract_assistant_text(out_text)
    artifacts.append(save_artifact(
        task.id, "last-message.md",
        (summary or "（无 assistant 文本输出，详见 kimi.jsonl）").encode(),
        "report"))

    # 收集工作区内 CLI 产出的文件（排除 logs/input/context）
    for path in sorted(ws.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ws)
        if rel.parts[0] in ("logs", "input", "artifacts") or \
                rel.name == "context.md":
            continue
        artifacts.append(
            save_artifact(task.id, f"workspace/{rel}", path.read_bytes(),
                          "file"))
    return artifacts
