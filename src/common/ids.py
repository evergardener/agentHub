"""Task ID generation per design doc §22.1.

Phase 3 起：ID 由 SQLite counters 表单事务生成（见 state.db.next_task_id）。
Phase 1–2（DB 未接入）：临时格式 T-<ULID 风格时间戳+随机>。
禁止"读最大值再 +1"。
"""

from __future__ import annotations

import secrets
from datetime import datetime


def temp_task_id(now: datetime | None = None) -> str:
    """Phase 1–2 临时 ID：T-YYYYMMDD-HHMMSS-<4字节随机十六进制>。"""
    now = now or datetime.now()
    return f"T-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def format_task_id(seq: int, now: datetime | None = None) -> str:
    """Phase 3 起正式格式：T-YYYYMMDD-XXXX。"""
    now = now or datetime.now()
    return f"T-{now:%Y%m%d}-{seq:04d}"


def counter_name(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"task:{now:%Y%m%d}"


def idempotency_key(task_id: str, attempt: int) -> str:
    """设计文档 §22.5：idempotency_key = task_id + ':' + attempt。"""
    return f"{task_id}:{attempt}"
