"""状态机迁移合法性测试（设计文档 §5.3）。"""

from common.models import (
    A2A_STATE_MAP,
    TERMINAL_STATES,
    TaskStatus,
    is_legal_transition,
)


def test_happy_path():
    chain = [
        TaskStatus.CREATED,
        TaskStatus.QUEUED,
        TaskStatus.ASSIGNED,
        TaskStatus.WORKING,
        TaskStatus.COMPLETED,
        TaskStatus.REVIEWED,
        TaskStatus.ACCEPTED,
    ]
    for src, dst in zip(chain, chain[1:]):
        assert is_legal_transition(src, dst), f"{src} -> {dst} should be legal"


def test_retry_loop():
    assert is_legal_transition(TaskStatus.FAILED, TaskStatus.RETRY_PENDING)
    assert is_legal_transition(TaskStatus.RETRY_PENDING, TaskStatus.QUEUED)


def test_rejected_rework():
    assert is_legal_transition(TaskStatus.REVIEWED, TaskStatus.WORKING)


def test_cancel_from_any_non_terminal():
    for status in TaskStatus:
        if status not in TERMINAL_STATES:
            assert is_legal_transition(status, TaskStatus.CANCELLED)


def test_illegal_transitions_rejected():
    # 迟到的 progress 不得覆盖 cancelled（§5.3 末尾规则的基础）
    assert not is_legal_transition(TaskStatus.CANCELLED, TaskStatus.WORKING)
    assert not is_legal_transition(TaskStatus.ACCEPTED, TaskStatus.WORKING)
    assert not is_legal_transition(TaskStatus.QUEUED, TaskStatus.COMPLETED)
    assert not is_legal_transition(TaskStatus.COMPLETED, TaskStatus.WORKING)


def test_terminal_states_have_no_outgoing():
    for status in TERMINAL_STATES:
        for dst in TaskStatus:
            assert not is_legal_transition(status, dst)


def test_a2a_mapping_covers_all_states():
    for status in TaskStatus:
        assert status in A2A_STATE_MAP
    assert A2A_STATE_MAP[TaskStatus.BLOCKED] == "input-required"
    assert A2A_STATE_MAP[TaskStatus.CANCELLED] == "canceled"
