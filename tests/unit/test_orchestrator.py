"""Phase 4 单元测试：registry / janitor / recovery / task_manager 取消级联。"""

from datetime import datetime, timedelta

import pytest

from common.models import TaskStatus
from orchestrator import state_store
from orchestrator.recovery import recover
from orchestrator.registry import Registry
from orchestrator.task_manager import TaskManager
from state import alert_store
from state.db import CST, init_db, next_task_id
from state.janitor import Janitor

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
    state_store.transition_task(conn, tid, TaskStatus.AWAITING_ACCEPTANCE)
    assert len(registry.find_agent_by_skill("coding", conn)) == 1


# ---------- janitor ----------


def test_janitor_fails_unclaimed_hermes_message_after_timeout(
        conn, monkeypatch):
    from orchestrator import collaboration_store, supervision_store
    from state import janitor as janitor_module

    context = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-stale-message",
        objective="stale message")
    tid = next_task_id(conn)
    state_store.create_task(
        conn, task_id=tid, objective="stale message", created_by="qishuo",
        assigned_to="codex", collaboration_id=context["collaboration_id"])
    supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-stale-message", task_id=tid)
    message = collaboration_store.append_user_message_to_hermes(
        conn, collaboration_id=context["collaboration_id"], user_id="user",
        content={"text": "不能一直显示排队"})
    supervision_store.enqueue_user_message(
        conn, collaboration_id=context["collaboration_id"],
        message_id=message["id"])
    old = (datetime.now(CST) - timedelta(minutes=10)).isoformat(
        timespec="seconds")
    conn.execute(
        "UPDATE supervision_outbox SET created_at = ? WHERE message_id = ?;",
        (old, message["id"]),
    )
    conn.commit()
    monkeypatch.setattr(
        janitor_module, "MESSAGE_DELIVERY_TIMEOUT_SECONDS", 300.0)
    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts, janitor.artifact_roots = conn, [], ()

    stats = janitor.sweep()

    assert stats["message_delivery_failed"] == 1
    assert conn.execute(
        "SELECT delivery_status FROM conversation_messages WHERE id = ?;",
        (message["id"],),
    ).fetchone()["delivery_status"] == "failed"
    assert conn.execute(
        "SELECT status FROM supervision_outbox WHERE message_id = ?;",
        (message["id"],),
    ).fetchone()["status"] == "failed"
    event = conn.execute(
        "SELECT payload_json FROM events"
        " WHERE event_type = 'conversation.message.delivery.updated'"
        " ORDER BY seq DESC LIMIT 1;"
    ).fetchone()
    assert "delivery_not_claimed_before_timeout" in event["payload_json"]


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


def test_janitor_requeues_blocked_dead_worker_and_notifies_supervisor(conn):
    from orchestrator import collaboration_store, supervision_store

    context = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-dead-lease",
        objective="blocked worker")
    tid = next_task_id(conn)
    state_store.create_task(
        conn, task_id=tid, objective="blocked worker", created_by="qishuo",
        assigned_to="codex", collaboration_id=context["collaboration_id"])
    state_store.transition_task(conn, tid, TaskStatus.ASSIGNED)
    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
    collaboration_store.bind_agent_session(
        conn, collaboration_id=context["collaboration_id"], task_id=tid,
        agent_id="codex", adapter_session_id="S-dead",
        adapter_instance_id="codex-dead")
    watch = supervision_store.register_watch(
        conn, peer="qishuo", context_id="ctx-dead-lease", task_id=tid)
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at,"
        " created_at, updated_at) VALUES"
        " ('codex','worker','offline','2000-01-01T00:00:00+08:00','x','x');")
    conn.commit()

    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = conn, []
    stats = janitor.sweep()

    assert stats["requeued"] == 1
    assert state_store.get_task(conn, tid)["status"] == "queued"
    binding = collaboration_store.get_current_agent_session(
        conn, tid, "codex")
    assert binding["status"] == "interrupted"
    assert binding["recovery_state"] == "worker_lease_expired"
    notifications = supervision_store.pull_notifications(
        conn, peer="qishuo", watch_ids=[watch["watch_id"]])
    assert "task.failed" in {
        item["event_type"] for item in notifications}
    event = conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ?"
        " AND event_type = 'task.failed';", (tid,)
    ).fetchone()
    assert "worker_lease_expired" in event["payload_json"]


def test_janitor_timeout_sweep(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    conn.execute(
        "UPDATE tasks SET started_at = '2000-01-01T00:00:00+08:00',"
        " updated_at = '2000-01-01T00:00:00+08:00',"
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


def test_janitor_pauses_timeout_while_blocked_and_resets_on_resume(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    first_started = "2000-01-01T00:00:00+08:00"
    conn.execute(
        "UPDATE tasks SET started_at = ?, updated_at = ?,"
        " timeout_seconds = 60 WHERE id = ?;",
        (first_started, first_started, tid))
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at,"
        " updated_at) VALUES"
        " ('codex','worker','online','2999-01-01T00:00:00+08:00','x','x');")
    conn.commit()

    state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
    blocked = state_store.get_task(conn, tid)
    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = conn, []
    assert janitor.sweep()["failed_timeout"] == 0
    assert state_store.get_task(conn, tid)["status"] == "blocked"

    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    resumed = state_store.get_task(conn, tid)
    assert resumed["started_at"] == first_started
    assert resumed["updated_at"] != blocked["updated_at"]
    assert janitor.sweep()["failed_timeout"] == 0
    assert state_store.get_task(conn, tid)["status"] == "working"

    first_resume_version = resumed["updated_at"]
    state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    second_resume = state_store.get_task(conn, tid)
    assert second_resume["started_at"] == first_started
    assert second_resume["updated_at"] != first_resume_version
    assert janitor.sweep()["failed_timeout"] == 0
    assert state_store.get_task(conn, tid)["status"] == "working"
    assert janitor.alerts == []


def test_janitor_sweeps_after_resumed_working_interval_expires(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    conn.execute(
        "UPDATE tasks SET timeout_seconds = 60 WHERE id = ?;", (tid,))
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at,"
        " updated_at) VALUES"
        " ('codex','worker','online','2999-01-01T00:00:00+08:00','x','x');")
    conn.commit()
    state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    conn.execute(
        "UPDATE tasks SET updated_at = '2000-01-01T00:00:00+08:00'"
        " WHERE id = ?;", (tid,))
    conn.commit()

    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = conn, []
    stats = janitor.sweep()

    assert stats["failed_timeout"] == 1
    assert state_store.get_task(conn, tid)["status"] == "failed"
    assert janitor.alerts[0]["kind"] == "timeout_swept"


def test_transition_snapshot_rejects_rapid_working_aba(conn):
    tid = _task(conn, status=TaskStatus.WORKING)
    original_version = state_store.get_task(conn, tid)["updated_at"]

    state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
    state_store.transition_task(conn, tid, TaskStatus.WORKING)
    resumed = state_store.get_task(conn, tid)
    assert resumed["updated_at"] != original_version

    with pytest.raises(state_store.IllegalTransition):
        state_store.transition_task(
            conn, tid, TaskStatus.FAILED,
            error_message="timeout_swept",
            expected_updated_at=original_version)
    current = state_store.get_task(conn, tid)
    assert current["status"] == "working"
    assert current["error_message"] is None


def test_janitor_stale_scan_cannot_fail_resumed_working_task(conn):
    tid = _task(conn, status=TaskStatus.WORKING, assigned_to="codex")
    stale_version = "2000-01-01T00:00:00+08:00"
    conn.execute(
        "UPDATE tasks SET started_at = ?, updated_at = ?,"
        " timeout_seconds = 60 WHERE id = ?;",
        (stale_version, stale_version, tid))
    conn.execute(
        "INSERT INTO agents (id, role, status, lease_expires_at, created_at,"
        " updated_at) VALUES"
        " ('codex','worker','online','2999-01-01T00:00:00+08:00','x','x');")
    conn.commit()

    class StaleRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            state_store.transition_task(conn, tid, TaskStatus.BLOCKED)
            state_store.transition_task(conn, tid, TaskStatus.WORKING)
            return self.rows

    class RaceConnection:
        backend = "sqlite"

        def execute(self, sql, params=()):
            cursor = conn.execute(sql, params)
            if "SELECT id, updated_at, timeout_seconds FROM tasks" in sql:
                return StaleRows(cursor.fetchall())
            return cursor

        def commit(self):
            conn.commit()

        def rollback(self):
            conn.rollback()

    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = RaceConnection(), []
    stats = janitor.sweep()

    current = state_store.get_task(conn, tid)
    assert current["status"] == "working"
    assert current["updated_at"] != stale_version
    assert current["error_message"] is None
    assert stats["failed_timeout"] == 0
    assert janitor.alerts == []


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
    janitor.artifact_roots = (tmp_path.resolve(),)

    missing = janitor.sweep()
    assert missing["artifact_alerts"] == 1
    assert len(alert_store.list_alerts(conn, status="open")) == 1

    artifact.write_text("recovered", encoding="utf-8")
    recovered = janitor.sweep()
    assert recovered["artifact_resolved"] == 1
    assert alert_store.list_alerts(conn, status="open") == []


def test_janitor_ignores_and_resolves_artifacts_outside_managed_roots(
        conn, tmp_path):
    tid = _task(conn, status=TaskStatus.COMPLETED)
    unmanaged = tmp_path / "historical-test" / "missing.md"
    conn.execute(
        "INSERT INTO artifacts (id, task_id, name, type, path, sha256,"
        " created_at) VALUES (?,?,?,?,?,?,?);",
        ("A-outside", tid, "missing.md", "report", str(unmanaged), "abc", "now"),
    )
    conn.commit()
    alert_store.upsert_alert(
        conn, kind="artifact_missing", severity="warning", source="janitor",
        task_id=tid, detail=str(unmanaged))
    janitor = Janitor.__new__(Janitor)
    janitor.conn, janitor.alerts = conn, []
    janitor.artifact_roots = ((tmp_path / "managed").resolve(),)

    stats = janitor.sweep()

    assert stats["artifact_ignored"] == 1
    assert stats["artifact_alerts"] == 0
    assert stats["artifact_resolved"] == 1
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
    for dst in (
        TaskStatus.ASSIGNED,
        TaskStatus.WORKING,
        TaskStatus.AWAITING_ACCEPTANCE,
    ):
        state_store.transition_task(tm.conn, tid, dst)
    assert tm.review_result(tid, approved=False, notes="建议修复") == "reviewed"
    assert tm.reject_result(tid, feedback="fix it") == "rework_pending"
    assert state_store.get_task(tm.conn, tid)["status"] == "rework_pending"


def test_acceptance_requires_user_and_rework_requires_feedback(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    tid = tm.create_task("review me")
    for dst in (
        TaskStatus.ASSIGNED,
        TaskStatus.WORKING,
        TaskStatus.AWAITING_ACCEPTANCE,
    ):
        state_store.transition_task(tm.conn, tid, dst)

    assert tm.review_result(tid, approved=True, notes="looks good") == "reviewed"
    assert state_store.get_task(tm.conn, tid)["status"] == "reviewed"
    with pytest.raises(ValueError, match="feedback"):
        tm.reject_result(tid, feedback="   ")
    assert tm.accept_result(tid, decided_by="user", via="webui") == "accepted"


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
