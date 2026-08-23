"""depends_on 依赖门控测试（设计文档 §5.1 / §5.3 / Phase 6 前置）。"""

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio


@pytest.fixture
def tm(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    return TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")


def test_dependent_task_waits_for_acceptance(tm):
    t1 = tm.create_task("research")
    t2 = tm.create_task("implement", depends_on=[t1])
    # T2 未满足依赖：保持 created，不进 queued
    assert state_store.get_task(tm.conn, t2)["status"] == "created"
    assert state_store.get_task(tm.conn, t1)["status"] == "queued"

    # T1 完成但未 accepted → T2 仍等待
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, t1, dst)
    tm.review_result(t1, approved=False, notes="redo")  # 返工 ≠ 解锁
    assert state_store.get_task(tm.conn, t2)["status"] == "created"

    # T1 accepted → T2 自动 promoted
    tm.accept_result(t1, decided_by="user")
    assert state_store.get_task(tm.conn, t2)["status"] == "queued"


def test_multiple_deps_all_required(tm):
    a = tm.create_task("a")
    b = tm.create_task("b")
    c = tm.create_task("c", depends_on=[a, b])
    for t in (a,):
        for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
            state_store.transition_task(tm.conn, t, dst)
        tm.review_result(t, approved=True)
        tm.accept_result(t, decided_by="user")
    # 只有一个依赖 accepted，c 不动
    assert state_store.get_task(tm.conn, c)["status"] == "created"
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, b, dst)
    tm.review_result(b, approved=True)
    tm.accept_result(b, decided_by="user")
    assert state_store.get_task(tm.conn, c)["status"] == "queued"


def test_cancelled_dep_blocks_forever(tm):
    t1 = tm.create_task("doomed")
    t2 = tm.create_task("blocked-forever", depends_on=[t1])
    tm.cancel_task(t1)
    assert tm.promote_dependents(t1) == []
    assert state_store.get_task(tm.conn, t2)["status"] == "created"
