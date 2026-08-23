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
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    REVIEWED = "reviewed"
    REWORK_PENDING = "rework_pending"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


class CollaborationPhase(str, enum.Enum):
    """User-visible phase layered above the existing execution state machine."""

    PLANNING = "planning"
    CLARIFYING = "clarifying"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    REWORK = "rework"
    ACCEPTED = "accepted"
    PAUSED = "paused"
    NEEDS_REPLAN = "needs_replan"
    CANCELLED = "cancelled"


class SenderType(str, enum.Enum):
    USER = "user"
    HERMES = "hermes"
    AGENT = "agent"
    SYSTEM = "system"


class ActionRisk(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class InterventionMode(str, enum.Enum):
    COMMENT = "comment"
    STEER = "steer"
    PAUSE = "pause"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"
    TAKEOVER = "takeover"
    RETURN_TO_HERMES = "return_to_hermes"


TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.ACCEPTED, TaskStatus.CANCELLED}
)
# failed 是否为终态取决于 retry_count 与 max_retries，由 task_manager 判断。

# 合法状态迁移表（设计文档 §5.3）
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.ASSIGNED, TaskStatus.CANCELLED}),
    TaskStatus.ASSIGNED: frozenset(
        {TaskStatus.WORKING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.WORKING: frozenset(
        {TaskStatus.BLOCKED, TaskStatus.AWAITING_ACCEPTANCE,
         TaskStatus.COMPLETED,  # backward-compatible historical path
         TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.WORKING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RETRY_PENDING, TaskStatus.CANCELLED}),
    TaskStatus.RETRY_PENDING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    # COMPLETED is retained for historical rows only.  New worker completion
    # events enter AWAITING_ACCEPTANCE instead.
    TaskStatus.COMPLETED: frozenset(
        {TaskStatus.REVIEWED, TaskStatus.REWORK_PENDING,
         TaskStatus.ACCEPTED, TaskStatus.CANCELLED}
    ),
    TaskStatus.AWAITING_ACCEPTANCE: frozenset(
        {TaskStatus.REVIEWED, TaskStatus.REWORK_PENDING,
         TaskStatus.ACCEPTED, TaskStatus.CANCELLED}
    ),
    TaskStatus.REVIEWED: frozenset(
        {TaskStatus.ACCEPTED, TaskStatus.REWORK_PENDING, TaskStatus.CANCELLED}
    ),
    TaskStatus.REWORK_PENDING: frozenset(
        {TaskStatus.ASSIGNED, TaskStatus.CANCELLED}
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
    TaskStatus.COMPLETED: "input-required",
    TaskStatus.AWAITING_ACCEPTANCE: "input-required",
    TaskStatus.REVIEWED: "input-required",
    TaskStatus.REWORK_PENDING: "working",
    TaskStatus.ACCEPTED: "completed",
    TaskStatus.FAILED: "failed",
    TaskStatus.CANCELLED: "canceled",
}
