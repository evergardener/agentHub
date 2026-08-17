"""A2A 客户端（Hermes 侧最小实现）— 设计文档 §11 a2a_client.py。

Phase 1 范围：Agent Card 获取、message/send、tasks/get。
"""

from __future__ import annotations

import uuid

import httpx


class A2aError(RuntimeError):
    pass


class A2aClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_agent_card(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/.well-known/agent-card.json")
            r.raise_for_status()
            return r.json()

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/health")
            r.raise_for_status()
            return r.json()

    async def send_message(
        self,
        text: str,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> dict:
        """发送任务消息，返回 A2A Task。"""
        metadata = {}
        if idempotency_key:
            metadata["idempotencyKey"] = idempotency_key
        if trace_id:
            metadata["traceId"] = trace_id
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                    "metadata": metadata,
                }
            },
        }
        return await self._rpc(payload)

    async def get_task(self, task_id: str) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        return await self._rpc(payload)

    async def _rpc(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/a2a", json=payload)
            r.raise_for_status()
            data = r.json()
        if "error" in data:
            raise A2aError(f"{data['error']['code']}: {data['error']['message']}")
        return data["result"]
