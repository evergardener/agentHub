"""Memory Service 接口抽象 — 设计文档 §15.3。

长期记忆的唯一入口。默认实现为 Hindsight（src/memory/hindsight_client.py），
替换实现时业务代码不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Memory:
    id: str
    content: str
    scope: str
    metadata: dict = field(default_factory=dict)
    created_at: str | None = None


class MemoryService(Protocol):
    def retain(self, content: str, scope: str, metadata: dict) -> str:
        """写入一条长期记忆。scope 例: user / project:<id> / system。返回 memory_id。"""
        ...

    def recall(
        self, query: str, scope: str | None = None, budget_tokens: int = 2048
    ) -> list[Memory]:
        """按相关性召回，带 token 预算。"""
        ...

    def reflect(self, topic: str) -> str | None:
        """可选：对某主题做归纳。实现不支持时返回 None。"""
        ...
