"""Hermes LLM 客户端 — OpenAI 兼容端点 + 工具调用（Evolution v3 §1）。

配置统一走 common.config（env-only，LAS_LLM_*）：
  LAS_LLM_BASE_URL  默认 http://127.0.0.1:8317/v1（本地 cliproxy）
  LAS_LLM_API_KEY   端点密钥
  LAS_LLM_MODEL     默认 deepseek-ai/DeepSeek-V4-Flash
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from common import config as cfg

DEFAULT_BASE = cfg.DEFAULT_LLM_BASE
DEFAULT_MODEL = cfg.DEFAULT_LLM_MODEL


class LLMFailed(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict = field(default_factory=dict)  # 原样回放进 messages


def _extract_message(resp: httpx.Response) -> dict:
    """兼容 9router/cliproxy 的 SSE 怪癖（同 adapters.kimi.runner）。"""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
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
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_acc.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""})
                acc["id"] = acc["id"] or tc.get("id", "")
                fn = tc.get("function") or {}
                acc["name"] = acc["name"] or fn.get("name", "")
                acc["arguments"] += fn.get("arguments", "")
        message = {"role": "assistant", "content": "".join(content_parts)}
        if tool_acc:
            message["tool_calls"] = [
                {"id": a["id"], "type": "function",
                 "function": {"name": a["name"], "arguments": a["arguments"]}}
                for a in (tool_acc[i] for i in sorted(tool_acc))
            ]
        return message
    return resp.json()["choices"][0]["message"]


class HermesLLM:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None):
        self.base_url = (base_url or cfg.llm_base_url()).rstrip("/")
        self.model = model or cfg.llm_model()
        self.api_key = api_key if api_key is not None else cfg.llm_api_key()

    async def chat(self, messages: list[dict],
                   tools: list[dict] | None = None,
                   timeout: float = 300) -> LLMReply:
        body: dict = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        if resp.status_code != 200:
            raise LLMFailed(f"llm {resp.status_code}: {resp.text[:300]}")
        message = _extract_message(resp)
        calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_parse_args(
                    (tc.get("function") or {}).get("arguments", "")),
            )
            for i, tc in enumerate(message.get("tool_calls") or [])
        ]
        return LLMReply(content=message.get("content") or "",
                        tool_calls=calls, raw_message=message)


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
