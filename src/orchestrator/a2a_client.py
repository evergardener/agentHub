"""A2A 客户端（Hermes 侧最小实现）— 设计文档 §11 a2a_client.py。

Phase 1 范围：Agent Card 获取、message/send、tasks/get。
Phase 5：支持经 agentgateway 访问（§3.4）——
  for_agent() 按 LAS_GATEWAY_URL（别名 AGENT_GATEWAY_URL）决定直连还是走
  gateway；走 gateway 时自动拼 /agents/<name> 前缀并注入 Bearer key
  （LAS_GATEWAY_API_KEY，common.config 统一读取）。
"""

from __future__ import annotations

import uuid

import httpx

from common import config as cfg


class A2aError(RuntimeError):
    pass


def _gateway_key() -> str:
    return cfg.gateway_api_key()


class A2aClient:
    def __init__(self, base_url: str, timeout: float = 30.0,
                 auth_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth_token = auth_token

    @classmethod
    def for_agent(cls, agent_name: str, direct_endpoint: str,
                  timeout: float = 30.0) -> "A2aClient":
        """按环境决定直连 adapter 还是经 agentgateway（Phase 5）。

        LAS_GATEWAY_URL 非空（如 http://127.0.0.1:8300）时：
        Hermes → gateway/agents/<name>，带 Bearer key；
        否则保持 Phase 1-4 行为：直连 direct_endpoint。
        """
        gw = cfg.gateway_url()
        if not gw:
            return cls(direct_endpoint, timeout=timeout)
        return cls(f"{gw.rstrip('/')}/agents/{agent_name}",
                   timeout=timeout, auth_token=_gateway_key())

    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}
        # adapter 侧鉴权（v3 加固）：与 gateway 的 Bearer 互不冲突；
        # 走 gateway 时该头被透传到后端 adapter。
        tok = cfg.adapter_token()
        if tok:
            h["X-Agent-Token"] = tok
        return h

    async def get_agent_card(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/.well-known/agent-card.json",
                                 headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/health",
                                 headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def send_message(
        self,
        text: str,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """发送任务消息，返回 A2A Task。"""
        metadata = {}
        if idempotency_key:
            metadata["idempotencyKey"] = idempotency_key
        if trace_id:
            metadata["traceId"] = trace_id
        if task_id:
            metadata["taskId"] = task_id
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

    async def send_and_wait(
        self,
        text: str,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
        timeout: float = 1800.0,
        poll_interval: float = 0.5,
    ) -> dict:
        """send 后轮询 tasks/get 直到终态（v3 异步 A2A）。

        单次 HTTP 调用都是秒级；任务本身可以跑任意时长。
        """
        import asyncio
        import time

        task = await self.send_message(
            text, idempotency_key=idempotency_key,
            trace_id=trace_id, task_id=task_id,
        )
        terminal = {"completed", "failed", "canceled", "rejected"}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if task["status"]["state"] in terminal:
                return task
            await asyncio.sleep(poll_interval)
            task = await self.get_task(task["id"])
        raise TimeoutError(f"send_and_wait {task['id']} exceeded {timeout}s")

    async def _rpc(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/a2a", json=payload,
                                  headers=self._headers())
            r.raise_for_status()
            data = r.json()
        if "error" in data:
            raise A2aError(f"{data['error']['code']}: {data['error']['message']}")
        return data["result"]
