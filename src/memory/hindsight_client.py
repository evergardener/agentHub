"""Hindsight 实现的 MemoryService — 设计文档 §15.3 / ADR-0002。

目标实例：用户本机已有 Hindsight 0.8.3（docker compose 部署）。
  API: http://127.0.0.1:18888（容器 hindsight-api，已启用 API Key 鉴权）

配置（Secrets 规则见设计文档 §14，密钥只从环境变量读取，env-only）：
  LAS_HINDSIGHT_URL      默认 http://127.0.0.1:18888（别名 HINDSIGHT_API_URL）
  LAS_HINDSIGHT_API_KEY  API Key（别名 HINDSIGHT_API_KEY）

scope → bank_id 映射：
  user            -> las-user
  project:<id>    -> las-project-<id>
  system          -> las-system
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error

from common.memory import Memory

DEFAULT_BASE_URL = "http://127.0.0.1:18888"

_SCOPE_BANK_MAP = {
    "user": "las-user",
    "system": "las-system",
}


def scope_to_bank(scope: str) -> str:
    if scope in _SCOPE_BANK_MAP:
        return _SCOPE_BANK_MAP[scope]
    if scope.startswith("project:"):
        return "las-project-" + scope.split(":", 1)[1]
    raise ValueError(f"unknown memory scope: {scope!r}")


class HindsightMemoryService:
    """MemoryService 的 Hindsight 实现（仅 Hermes 侧使用）。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        from common import config as cfg

        self.base_url = (base_url or cfg.hindsight_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else cfg.hindsight_api_key()

    # ---- MemoryService 接口 ----

    def retain(self, content: str, scope: str, metadata: dict) -> str:
        bank = scope_to_bank(scope)
        body = {"items": [{"content": content, "metadata": metadata}]}
        resp = self._request("POST", f"/v1/default/banks/{bank}/memories", body)
        # 0.8.3 返回操作结果；id 取首项（若提供）
        items = resp.get("items") if isinstance(resp, dict) else None
        if items and isinstance(items, list) and items[0].get("id"):
            return items[0]["id"]
        return json.dumps(resp, ensure_ascii=False)

    def recall(
        self, query: str, scope: str | None = None, budget_tokens: int = 2048
    ) -> list[Memory]:
        bank = scope_to_bank(scope or "user")
        body = {"query": query, "budget_tokens": budget_tokens}
        resp = self._request(
            "POST", f"/v1/default/banks/{bank}/memories/recall", body
        )
        results = resp.get("results", []) if isinstance(resp, dict) else []
        return [
            Memory(
                id=str(r.get("id", "")),
                content=r.get("content", r.get("text", "")),
                scope=scope or "user",
                metadata=r.get("metadata", {}),
                created_at=r.get("created_at"),
            )
            for r in results
        ]

    def reflect(self, topic: str) -> str | None:
        bank = scope_to_bank("system")
        resp = self._request(
            "POST", f"/v1/default/banks/{bank}/reflect", {"query": topic}
        )
        if isinstance(resp, dict):
            return resp.get("response") or resp.get("text")
        return None

    # ---- 内部 ----

    def _request(self, method: str, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"hindsight {method} {path} -> {e.code}: {detail}") from e
