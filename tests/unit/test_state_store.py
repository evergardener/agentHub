"""state_store 单元测试（设计文档 §5.3 / §22.3 / §17.4）。"""

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from state.db import init_db, next_task_id


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "state.db")


def _make_task(conn, status=TaskStatus.QUEUED):
    tid = next_task_id(conn)
    state_store.create_task(
        conn, task_id=tid, objective="obj", created_by="hermes", status=status
    )
    return tid


def test_create_and_get(conn):
    tid = _make_task(conn)
    row = state_store.get_task(conn, tid)
    assert row["status"] == "queued"
    assert row["retry_count"] == 0


def test_legal_transition_chain(conn):
    tid = _make_task(conn)
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED,
                TaskStatus.REVIEWED, TaskStatus.ACCEPTED):
        state_store.transition_task(conn, tid, dst)
    assert state_store.get_task(conn, tid)["status"] == "accepted"


def test_illegal_transition_rejected(conn):
    tid = _make_task(conn)
    with pytest.raises(state_store.IllegalTransition):
        state_store.transition_task(conn, tid, TaskStatus.COMPLETED)  # queued→completed 非法


def test_failed_increments_retry_count(conn):
    tid = _make_task(conn, status=TaskStatus.WORKING)
    state_store.transition_task(conn, tid, TaskStatus.FAILED)
    assert state_store.get_task(conn, tid)["retry_count"] == 1
    # failed → retry_pending → queued → assigned → working 重试链
    state_store.transition_task(conn, tid, TaskStatus.RETRY_PENDING)
    state_store.transition_task(conn, tid, TaskStatus.QUEUED)
    state_store.transition_task(conn, tid, TaskStatus.ASSIGNED)
    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    assert state_store.get_task(conn, tid)["status"] == "working"


def test_idempotent_same_state(conn):
    tid = _make_task(conn)
    state_store.transition_task(conn, tid, TaskStatus.ASSIGNED)
    state_store.transition_task(conn, tid, TaskStatus.ASSIGNED)  # 重复事件不报错
    assert state_store.get_task(conn, tid)["status"] == "assigned"


def test_duplicate_event_rejected(conn):
    event = {"event_id": "E-1", "event_type": "task.started",
             "task_id": "T-x", "source": "codex", "payload": {}}
    state_store.record_event(conn, event)
    with pytest.raises(state_store.DuplicateEvent):
        state_store.record_event(conn, event)


def test_heartbeat_sets_lease(conn):
    state_store.update_heartbeat(conn, "codex", lease_ttl_seconds=90)
    row = conn.execute("SELECT * FROM agents WHERE id='codex';").fetchone()
    assert row["status"] == "online"
    assert row["last_seen_at"] is not None
    assert row["lease_expires_at"] > row["last_seen_at"]


def test_task_id_uses_counters(conn):
    a = next_task_id(conn)
    b = next_task_id(conn)
    assert a.endswith("-0001") and b.endswith("-0002")
