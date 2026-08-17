"""Phase 8：agentctl 的 events / retry / cancel 命令。"""

from __future__ import annotations

import sys

import pytest

from cli.agentctl import main
from common.models import TaskStatus
from orchestrator.task_manager import TaskManager
from orchestrator import state_store


@pytest.fixture
def env(tmp_path, monkeypatch):
    """返回 (TaskManager, run_cli)；run_cli(*args) 等价于 agentctl --db <db> *args。"""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    db = tmp_path / "state.db"
    tm = TaskManager(db_path=db, workspace=tmp_path)

    def run_cli(*args: str) -> int:
        monkeypatch.setattr(sys, "argv",
                            ["agentctl", "--db", str(db), *args])
        return main()

    return tm, run_cli


def test_events_dump_and_filter(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("task one")  # create_task 会写事件
    state_store.record_event(tm.conn, {
        "event_id": "e-x", "subject": "task.custom", "task_id": t1,
        "agent_id": None, "event_type": "task.custom",
        "payload_json": "{}", "created_at": "2026-08-17T00:00:00",
    })
    assert run_cli("events") == 0
    out = capsys.readouterr().out
    assert "task.custom" in out and t1 in out

    assert run_cli("events", "--type", "task.custom") == 0
    out = capsys.readouterr().out
    assert "task.custom" in out


def test_task_retry(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("will fail")
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED,
                TaskStatus.WORKING, TaskStatus.FAILED):
        state_store.transition_task(tm.conn, t1, dst)
    assert run_cli("task", "retry", t1) == 0
    assert state_store.get_task(tm.conn, t1)["status"] == "queued"
    assert "queued" in capsys.readouterr().out


def test_task_retry_rejects_non_failed(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("still fresh")
    assert run_cli("task", "retry", t1) == 1
    assert "retry failed" in capsys.readouterr().out


def test_task_cancel_cascades(env, capsys):
    tm, run_cli = env
    parent = tm.create_task("parent")
    child = tm.create_task("child", parent_id=parent)
    assert run_cli("task", "cancel", parent) == 0
    assert state_store.get_task(tm.conn, parent)["status"] == "cancelled"
    assert state_store.get_task(tm.conn, child)["status"] == "cancelled"
    assert "2 task(s)" in capsys.readouterr().out


def test_task_cancel_terminal_is_noop(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("done")
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.WORKING,
                TaskStatus.COMPLETED, TaskStatus.REVIEWED, TaskStatus.ACCEPTED):
        state_store.transition_task(tm.conn, t1, dst)
    assert run_cli("task", "cancel", t1) == 1
    assert "nothing to cancel" in capsys.readouterr().out
