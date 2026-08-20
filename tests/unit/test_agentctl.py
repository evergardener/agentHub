"""Phase 8：agentctl 的 events / retry / cancel 命令。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

from cli.agentctl import main
from common.models import TaskStatus
from orchestrator.task_manager import TaskManager
from orchestrator import state_store
from state.db import CST


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


def test_agent_list_merges_catalog_and_live_lease(env, capsys, tmp_path):
    tm, run_cli = env
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(
        "agents:\n"
        "  codex:\n    enabled: true\n    endpoint: http://static:8201\n"
        "  dsh:\n    enabled: false\n    endpoint: http://static:8203\n",
        encoding="utf-8")
    state_store.update_heartbeat(
        tm.conn, "codex", endpoint="http://live:8201",
        lease_ttl_seconds=90)
    state_store.update_heartbeat(
        tm.conn, "kimi", endpoint="http://stale:8202",
        lease_ttl_seconds=1)
    tm.conn.execute(
        "UPDATE agents SET lease_expires_at = ? WHERE id = 'kimi';",
        ((datetime.now(CST) - timedelta(seconds=10)).isoformat(
            timespec="seconds"),))
    tm.conn.commit()

    assert run_cli(
        "--agents-file", str(agents_file), "agent", "list") == 0
    output = capsys.readouterr().out
    lines = {line.split()[0]: line for line in output.splitlines()[1:]}
    assert "online" in lines["codex"]
    assert "disabled" in lines["dsh"]
    assert "offline" in lines["kimi"]


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


# ---------- 审批闭环（blocked → input-required → 用户决策） ----------

def _to_blocked(tm, task_id: str) -> None:
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED,
                TaskStatus.WORKING, TaskStatus.BLOCKED):
        state_store.transition_task(tm.conn, task_id, dst)


def test_task_approve_resumes(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("dangerous op")
    _to_blocked(tm, t1)
    assert run_cli("task", "approve", t1, "--notes", "looks safe") == 0
    assert state_store.get_task(tm.conn, t1)["status"] == "working"
    assert "approved by user" in capsys.readouterr().out


def test_task_reject_cancels(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("dangerous op")
    _to_blocked(tm, t1)
    assert run_cli("task", "reject", t1) == 0
    assert state_store.get_task(tm.conn, t1)["status"] == "cancelled"
    assert "rejected by user" in capsys.readouterr().out


def test_task_approve_rejects_non_blocked(env, capsys):
    tm, run_cli = env
    t1 = tm.create_task("fresh")
    assert run_cli("task", "approve", t1) == 1
    assert "approve failed" in capsys.readouterr().out


# ---------- 长期记忆挂钩（Hermes 唯一写方） ----------

class FakeMemory:
    def __init__(self):
        self.retained: list[dict] = []

    def retain(self, content, scope, metadata):
        self.retained.append(
            {"content": content, "scope": scope, "metadata": metadata})
        return "mem-1"


def test_accept_retains_outcome(env):
    tm, _ = env
    mem = FakeMemory()
    tm.memory = mem
    t1 = tm.create_task("fix the bug", project="demo")
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.WORKING,
                TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, t1, dst)
    tm.review_result(t1, approved=True, notes="LGTM")
    assert len(mem.retained) == 1
    entry = mem.retained[0]
    assert entry["scope"] == "project:demo"
    assert "fix the bug" in entry["content"]
    assert entry["metadata"]["task_id"] == t1


def test_reject_does_not_retain(env):
    tm, _ = env
    mem = FakeMemory()
    tm.memory = mem
    t1 = tm.create_task("bad work")
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.WORKING,
                TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, t1, dst)
    tm.review_result(t1, approved=False)
    assert mem.retained == []


def test_memory_failure_does_not_block(env):
    tm, _ = env

    class BrokenMemory:
        def retain(self, *a, **k):
            raise ConnectionError("hindsight down")

    tm.memory = BrokenMemory()
    t1 = tm.create_task("task")
    for dst in (TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.WORKING,
                TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, t1, dst)
    # 记忆服务故障不影响验收
    assert tm.review_result(t1, approved=True) == "accepted"
