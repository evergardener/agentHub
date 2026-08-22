"""Phase 4 单元测试：registry / janitor / recovery / task_manager 取消级联。"""

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.recovery import recover
from orchestrator.registry import Registry
from state import alert_store
from state.db import init_db, next_task_id
from state.janitor import Janitor
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio

AGENTS_YAML = """
agents:
  codex:
    role: worker
    enabled: true
    endpoint: http://127.0.0.1:8201
    max_concurrent_tasks: 1
    skills: [coding, testing]
  kimi:
    role: worker
    enabled: true
    endpoint: http://127.0.0.1:8202
    max_concurrent_tasks: 2
    skills: [research]
  disabled_one:
    role: worker
    enabled: false
    endpoint: http://127.0.0.1:9999
    skills: [coding]
"""


@pytest.fixture
def registry(tmp_path):
    p = tmp_path / "agents.yaml"
    p.write_text(AGENTS_YAML)
    return Registry(p)


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "state.db")


def _task(conn, status=TaskStatus.QUEUED, assigned_to=None):
    tid = next_task_id(conn)
    state_store.create_task(conn, task_id=tid, objective="x",
                            created_by="hermes", status=status,
                            assigned_to=assigned_to)
    return tid


# ---------- registry ----------


def test_find_by_skill(registry, conn):
    found = registry.find_agent_by_skill("coding", conn)
    assert [a.id for a in found] == ["codex"]  # disabled_one 被过滤
    assert [a.id for a in registry.find_agent_by_skill("research", conn)] == ["kimi"]


def test_capacity_check(registry, conn):
    # codex max=1，塞一个 working 任务后应被容量过滤
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    assert registry.find_agent_by_skill("coding", conn) == []
    # kimi max=2，无任务 → 仍在
    assert registry.find_agent_by_skill("research", conn) != []
    # 不带 conn 时只做技能过滤
    assert len(registry.find_agent_by_skill("coding")) == 1
    state_store.transition_task(conn, tid, TaskStatus.COMPLETED)
    assert len(registry.find_agent_by_skill("coding", conn)) == 1


# ---------- janitor ----------


def test_janitor_requeues_dead_lease(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    conn.execute(
        "UPDATE tasks SET started_at = datetime('now') WHERE id = ?;", (tid,))
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at, updated_at)"
        " VALUES ('codex','worker','offline','2000-01-01T00:00:00+08:00','x','x');")
    conn.commit()
    j = Janitor.__new__(Janitor)
    j.conn, j.alerts = conn, []
    stats = j.sweep()
    assert stats["requeued"] == 1
    assert state_store.get_task(conn, tid)["status"] == "queued"
    assert j.alerts


def test_janitor_timeout_sweep(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    conn.execute(
        "UPDATE tasks SET started_at = '2000-01-01T00:00:00+08:00',"
        " timeout_seconds = 60 WHERE id = ?;", (tid,))
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at, updated_at)"
        " VALUES ('codex','worker','online','2999-01-01T00:00:00+08:00','x','x');")
    conn.commit()
    j = Janitor.__new__(Janitor)
    j.conn, j.alerts = conn, []
    stats = j.sweep()
    assert stats["failed_timeout"] == 1
    assert state_store.get_task(conn, tid)["status"] == "failed"
    assert "timeout" in state_store.get_task(conn, tid)["error_message"]


def test_janitor_cascade_cancel(conn):
    parent = _task(conn, status=TaskStatus.WORKING)
    child = next_task_id(conn)
    state_store.create_task(conn, task_id=child, objective="child",
                            created_by="hermes", status=TaskStatus.WORKING,
                            parent_id=parent)
    state_store.transition_task(conn, parent, TaskStatus.CANCELLED)
    j = Janitor.__new__(Janitor)
    j.conn, j.alerts = conn, []
    stats = j.sweep()
    assert stats["cascade_cancelled"] == 1
    assert state_store.get_task(conn, child)["status"] == "cancelled"


def test_janitor_resolves_artifact_alert_after_file_recovers(conn, tmp_path):
    tid = _task(conn, status=TaskStatus.COMPLETED)
    artifact = tmp_path / "result.md"
    conn.execute(
        "INSERT INTO artifacts (id, task_id, name, type, path, sha256,"
        " created_at) VALUES (?,?,?,?,?,?,?);",
        ("A-1", tid, "result.md", "report", str(artifact), "abc", "now"),
    )
    conn.commit()
    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = conn, []

    missing = janitor.sweep()
    assert missing["artifact_alerts"] == 1
    assert len(alert_store.list_alerts(conn, status="open")) == 1

    artifact.write_text("recovered", encoding="utf-8")
    recovered = janitor.sweep()
    assert recovered["artifact_resolved"] == 1
    assert alert_store.list_alerts(conn, status="open") == []


# ---------- task_manager 级联取消 ----------


def test_cancel_cascade(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    root = tm.create_task("root task")
    child = tm.create_task("child task", parent_id=root)
    grandchild = tm.create_task("grandchild task", parent_id=child)
    n = tm.cancel_task(root)
    assert n == 3
    for tid in (root, child, grandchild):
        assert state_store.get_task(tm.conn, tid)["status"] == "cancelled"


def test_review_rework_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    tid = tm.create_task("review me")
    for dst in (TaskStatus.ASSIGNED, TaskStatus.WORKING, TaskStatus.COMPLETED):
        state_store.transition_task(tm.conn, tid, dst)
    assert tm.review_result(tid, approved=False, notes="fix it") == "working"
    # 返工完成后再审，批准
    state_store.transition_task(tm.conn, tid, TaskStatus.COMPLETED)
    assert tm.review_result(tid, approved=True) == "accepted"


# ---------- recovery ----------


async def test_recovery_requeues_when_lease_expired(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="ghost")
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at, updated_at)"
        " VALUES ('ghost','worker','offline','2000-01-01T00:00:00+08:00','x','x');")
    conn.commit()
    stats = await recover(conn, endpoints={})  # ghost 无 endpoint → 等待? 不，无 endpoint 计 waiting
    # ghost 不在 endpoints → waiting；把 endpoint 给一个不可达地址再测
    stats = await recover(conn, endpoints={"ghost": "http://127.0.0.1:1"})
    assert stats["requeued"] == 1
    assert state_store.get_task(conn, tid)["status"] == "queued"
