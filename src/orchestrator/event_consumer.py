"""Hermes 事件消费者 — 设计文档 §8 hermes-orchestrator durable consumer。

Phase 2 形态：订阅 AGENT_EVENTS，把任务事件追加到
  $AGENT_WORKSPACE/logs/hermes-events.jsonl
作为 Hermes 的事件输入（替代轮询）。Phase 3 起由 State Writer 落 SQLite，
Phase 4 接入 Hermes 的 event-driven handler。

运行：PYTHONPATH=src python -m orchestrator.event_consumer
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from common import config as cfg
from orchestrator.nats_client import durable_consume, ensure_stream

DURABLE = "hermes-orchestrator"


def _event_log() -> Path:
    path = cfg.workspace() / "logs" / "hermes-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def log_handler(event: dict) -> None:
    with _event_log().open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


async def main() -> None:
    nats_url = cfg.nats_url()
    await ensure_stream(nats_url)
    await durable_consume(DURABLE, log_handler, nats_url)


if __name__ == "__main__":
    asyncio.run(main())
