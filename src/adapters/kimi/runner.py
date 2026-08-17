"""Kimi 运行时 — 设计文档 §Phase 6：研究 / 长上下文 Worker。

通过 OpenAI 兼容端点调用模型：
  KIMI_API_BASE     默认 http://127.0.0.1:8317/v1（本地 cliproxy → siliconflow）
  KIMI_MODEL        默认 deepseek-ai/DeepSeek-V4-Flash
  CLIPROXY_API_KEY  缺省从 Keychain agent-system/cliproxy-api-key 注入
                    （刻意不读 KIMI_API_KEY：与 Kimi Work 桌面端注入的同名变量冲突）

权限边界（§13 Kimi）：shell/ssh denied —— 本 runner 只发 HTTP，不执行命令。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import httpx

from adapters.common import A2aTask, save_artifact

DEFAULT_BASE = "http://127.0.0.1:8317/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


class KimiFailed(RuntimeError):
    pass


def _api_key() -> str:
    # 注意：不要读 KIMI_API_KEY —— Kimi Work 桌面端会注入同名变量（指向其自有
    # 网关），会造成 401。这里只认 CLIPROXY_API_KEY，缺省回落到 Keychain。
    key = os.environ.get("CLIPROXY_API_KEY")
    if key:
        return key
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "agent-system",
             "-a", "cliproxy-api-key", "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return ""


def _extract_content(resp: httpx.Response) -> str:
    """从响应中提取文本；兼容 9router 的 SSE 怪癖。

    9router 即使未请求 stream 也可能返回 text/event-stream，且 chunk 之间
    偶尔缺少 \n\n 分隔（`...}data: {...}`），因此先规范化再逐段解析。
    """
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        parts: list[str] = []
        normalized = resp.text.replace("data: ", "\ndata: ")
        for line in normalized.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue  # 与前后 chunk 粘连的残段，跳过
            choice = (chunk.get("choices") or [{}])[0]
            parts.append(
                choice.get("delta", {}).get("content")
                or choice.get("message", {}).get("content")
                or ""
            )
        return "".join(parts)
    return resp.json()["choices"][0]["message"]["content"]


async def run(task: A2aTask) -> list[dict]:
    base = os.environ.get("KIMI_API_BASE", DEFAULT_BASE)
    model = os.environ.get("KIMI_MODEL", DEFAULT_MODEL)
    prompt = (
        "你是一名研究助手。请针对以下任务给出结构化分析"
        "（要点、风险、建议），用中文回答：\n\n" + task.objective
    )
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key()}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    if resp.status_code != 200:
        raise KimiFailed(f"llm call failed {resp.status_code}: {resp.text[:300]}")
    content = _extract_content(resp)
    if not content.strip():
        raise KimiFailed(
            f"llm returned empty content (ct={resp.headers.get('content-type')},"
            f" body={resp.text[:200]!r})"
        )
    artifact = save_artifact(
        task.id, "analysis.md",
        f"# {task.objective}\n\n{content}\n".encode("utf-8"),
        artifact_type="report",
    )
    return [artifact]
