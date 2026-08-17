"""Event envelope per design doc §7.2."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


@dataclass
class Event:
    event_type: str
    source: str
    task_id: str | None = None
    trace_id: str | None = None
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"E-{uuid.uuid4()}")
    timestamp: str = field(
        default_factory=lambda: datetime.now(CST).isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            timestamp=data["timestamp"],
            source=data["source"],
            task_id=data.get("task_id"),
            trace_id=data.get("trace_id"),
            payload=data.get("payload", {}),
        )
