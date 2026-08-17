"""Common models: task status machine per design doc §5.2 / §5.3."""

from __future__ import annotations

import enum


class TaskStatus(str, enum.Enum):
    CREATED = "created"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    WORKING = "working"
    BLOCKED = "blocked"
    FAILED = "failed"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
    REVIEWED = "reviewed"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.ACCEPTED, TaskStatus.CANCELLED}
)
# failed 是否为终态取决于 retry_count 与 max_retries，由 task_manager 判断。

# 合法状态迁移表（设计文档 §5.3）
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.ASSIGNED, TaskStatus.CANCELLED}),
    TaskStatus.ASSIGNED: frozenset({TaskStatus.WORKING, TaskStatus.CANCELLED}),
    TaskStatus.WORKING: frozenset(
        {TaskStatus.BLOCKED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.WORKING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RETRY_PENDING, TaskStatus.CANCELLED}),
    TaskStatus.RETRY_PENDING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset({TaskStatus.REVIEWED, TaskStatus.CANCELLED}),
    TaskStatus.REVIEWED: frozenset(
        {TaskStatus.ACCEPTED, TaskStatus.WORKING, TaskStatus.CANCELLED}
    ),
    TaskStatus.ACCEPTED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def is_legal_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    """校验迁移是否合法（§5.3）。State Writer 应用事件前必须调用。"""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


# 内部状态 → A2A TaskState 映射（设计文档 §5.4）
A2A_STATE_MAP: dict[TaskStatus, str] = {
    TaskStatus.CREATED: "submitted",
    TaskStatus.QUEUED: "submitted",
    TaskStatus.ASSIGNED: "submitted",
    TaskStatus.WORKING: "working",
    TaskStatus.RETRY_PENDING: "working",
    TaskStatus.BLOCKED: "input-required",
    TaskStatus.COMPLETED: "completed",
    TaskStatus.REVIEWED: "completed",
    TaskStatus.ACCEPTED: "completed",
    TaskStatus.FAILED: "failed",
    TaskStatus.CANCELLED: "canceled",
}
