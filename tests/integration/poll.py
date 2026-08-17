"""异步 A2A 轮询辅助（v3 M1）：message/send 立即返回后，轮询到终态。"""

from __future__ import annotations

import asyncio
import time

import httpx

TERMINAL = {"completed", "failed", "canceled", "rejected"}


async def wait_terminal(client: httpx.AsyncClient, task_id: str,
                        timeout: float = 600.0,
                        poll_interval: float = 0.5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.post("/a2a", json={
            "jsonrpc": "2.0", "id": "poll", "method": "tasks/get",
            "params": {"id": task_id},
        })
        task = r.json()["result"]
        if task["status"]["state"] in TERMINAL:
            return task
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"task {task_id} not terminal within {timeout}s")


async def send_and_wait(client: httpx.AsyncClient, text: str,
                        metadata: dict | None = None,
                        timeout: float = 600.0) -> dict:
    r = await client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "send", "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "metadata": metadata or {},
        }},
    })
    task = r.json()["result"]
    if task["status"]["state"] in TERMINAL:
        return task
    return await wait_terminal(client, task["id"], timeout=timeout)
