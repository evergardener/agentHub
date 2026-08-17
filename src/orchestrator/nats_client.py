"""NATS / JetStream 客户端 — 设计文档 §11 nats_client.py / §8。

提供：
  - ensure_stream：幂等创建 AGENT_EVENTS 流
  - durable_consume：durable consumer 循环（hermes-orchestrator 等）
  - replay_spool：本地暂存事件重发（§17.7）

连接策略注意：发布方（短连接）必须 max_reconnect_attempts=1（见
adapters/common.py 的教训）；消费方（长连接）允许重连。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

import nats
from nats.js.api import StreamConfig

STREAM_NAME = "AGENT_EVENTS"
STREAM_SUBJECTS = ["agent.*.*", "task.*", "artifact.*", "system.*"]
DEFAULT_URL = "nats://127.0.0.1:4222"
MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 天，config/nats.yaml


async def ensure_stream(nats_url: str = DEFAULT_URL):
    nc = await nats.connect(
        nats_url, connect_timeout=2,
        max_reconnect_attempts=1, allow_reconnect=False,
    )
    try:
        js = nc.jetstream()
        try:
            await js.stream_info(STREAM_NAME)
        except Exception:
            await js.add_stream(StreamConfig(
                name=STREAM_NAME,
                subjects=STREAM_SUBJECTS,
                max_age=MAX_AGE_SECONDS,
            ))
        return js
    finally:
        await nc.close()


EventHandler = Callable[[dict], Awaitable[None]]


async def durable_consume(
    durable: str,
    handler: EventHandler,
    nats_url: str = DEFAULT_URL,
    stop_event: asyncio.Event | None = None,
) -> None:
    """durable consumer 主循环：手动 ACK，断线重连，stop_event 置位后退出。"""
    while stop_event is None or not stop_event.is_set():
        try:
            nc = await nats.connect(nats_url, connect_timeout=2)
            js = nc.jetstream()
            sub = await js.pull_subscribe(">", durable=durable, stream=STREAM_NAME)
            while stop_event is None or not stop_event.is_set():
                try:
                    msgs = await sub.fetch(batch=10, timeout=2)
                except nats.errors.TimeoutError:
                    continue
                for msg in msgs:
                    try:
                        event = json.loads(msg.data.decode("utf-8"))
                        await handler(event)
                        await msg.ack()
                    except Exception:
                        # 不 ACK，等待重投（§17.6）
                        await msg.nak()
        except Exception:
            await asyncio.sleep(2)  # NATS 不在，等待重连


async def replay_spool(spool_path: str | Path,
                       nats_url: str = DEFAULT_URL) -> int:
    """把暂存事件逐条发布到 JetStream，成功后归档 spool 文件。返回重放条数。"""
    spool = Path(spool_path)
    if not spool.exists() or spool.stat().st_size == 0:
        return 0
    nc = await nats.connect(
        nats_url, connect_timeout=2,
        max_reconnect_attempts=1, allow_reconnect=False,
    )
    count = 0
    try:
        js = nc.jetstream()
        lines = spool.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            await js.publish(event["event_type"], line.encode("utf-8"))
            count += 1
    finally:
        await nc.close()
    archive = spool.with_suffix(f".replayed-{int(asyncio.get_event_loop().time())}.jsonl")
    spool.rename(archive)
    return count
