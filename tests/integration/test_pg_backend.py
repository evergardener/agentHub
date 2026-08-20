"""M3 验收：PostgreSQL 后端核心路径（Evolution v3 §4）。

门控：LAS_TEST_PG=1 且 LAS_TEST_PG_URL 指向可用 PG
（缺省 postgresql://agenthub:agenthub-dev-only@127.0.0.1:5432，即 compose 栈）。
每个用例使用独立数据库（agenthub_test_<pid>），结束即删。
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.anyio

PG_ADMIN_URL = os.environ.get(
    "LAS_TEST_PG_URL",
    "postgresql://agenthub:agenthub-dev-only@127.0.0.1:5432/postgres",
)

requires_pg = pytest.mark.skipif(
    os.environ.get("LAS_TEST_PG") != "1",
    reason="set LAS_TEST_PG=1 (and compose postgres up) to run PG backend tests",
)


@pytest.fixture
def pg_url():
    import psycopg

    dbname = "agenthub_test_" + uuid.uuid4().hex[:8]
    admin = psycopg.connect(PG_ADMIN_URL, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        admin.close()
    base = PG_ADMIN_URL.rsplit("/", 1)[0]
    url = f"{base}/{dbname}"
    yield url
    admin = psycopg.connect(PG_ADMIN_URL, autocommit=True)
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        admin.close()


@requires_pg
def test_pg_migrations_and_core_flow(pg_url):
    from state.db import init_db, next_task_id

    conn = init_db(pg_url)

    # 迁移：全部版本已应用，含 Profile、Session recovery 与 Task Plan
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version;").fetchall()]
    assert versions == list(range(1, 11))

    from orchestrator import collaboration_store

    conversation_id = collaboration_store.create_conversation(
        conn, title="pg persistence")
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id, objective="verify pg migration")
    message = collaboration_store.append_message(
        conn, conversation_id=conversation_id,
        collaboration_id=collaboration_id, sender_type="hermes",
        sender_id="hermes", content={"text": "hello"},
        based_on_revision=1)
    assert message["sequence"] == 1

    # 任务 ID 计数器（ON CONFLICT ... RETURNING 方言分支）
    t1 = next_task_id(conn)
    t2 = next_task_id(conn)
    assert t1 != t2 and t1.endswith("0001") and t2.endswith("0002")

    # 任务生命周期 + int/str 双下标行访问
    from common.models import TaskStatus
    from orchestrator import state_store

    state_store.create_task(conn, task_id=t1, objective="pg backend 验证",
                            created_by="test", status=TaskStatus.CREATED)
    row = state_store.get_task(conn, t1)
    assert row["objective"] == "pg backend 验证"
    state_store.transition_task(conn, t1, TaskStatus.QUEUED)
    state_store.transition_task(conn, t1, TaskStatus.ASSIGNED)
    row = conn.execute("SELECT status FROM tasks WHERE id = ?;",
                       (t1,)).fetchone()
    assert row[0] == "assigned"  # int 下标
    assert row["status"] == "assigned"  # str 下标

    # 事件：seq 单调 + 重复去重
    state_store.record_event(conn, {"event_id": "e1", "event_type": "task.x",
                                    "task_id": t1})
    state_store.record_event(conn, {"event_id": "e2", "event_type": "task.x",
                                    "task_id": t1})
    with pytest.raises(state_store.DuplicateEvent):
        state_store.record_event(conn, {"event_id": "e1",
                                        "event_type": "task.x", "task_id": t1})
    seqs = [r[0] for r in conn.execute(
        "SELECT seq FROM events WHERE id IN ('e1', 'e2')"
        " ORDER BY seq;").fetchall()]
    assert len(seqs) == 2 and seqs[1] > seqs[0]

    # 心跳注册（endpoint/skills）
    state_store.update_heartbeat(conn, "codex", lease_ttl_seconds=90,
                                 endpoint="http://x:8201", skills=["coding"])
    agent = conn.execute("SELECT endpoint, skills_json FROM agents"
                         " WHERE id = 'codex';").fetchone()
    assert agent["endpoint"] == "http://x:8201"
    assert "coding" in agent["skills_json"]

    # 常驻授权（RETURNING id 方言分支）+ 撤销
    from hermes.policy import ApprovalPolicy

    gid = ApprovalPolicy.grant(conn, "重启", note="pg")
    assert gid >= 1
    assert ApprovalPolicy.revoke(conn, gid) is True

    conn.close()


@requires_pg
def test_pg_event_sequence_is_safe_across_concurrent_writers(pg_url):
    from orchestrator import state_store
    from state.db import connect, init_db

    init_db(pg_url).close()

    def write(index: int) -> None:
        conn = connect(pg_url)
        try:
            state_store.record_event(conn, {
                "event_id": f"E-concurrent-{index}",
                "event_type": "task.concurrent",
                "source": "test",
            })
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(96)))

    conn = connect(pg_url)
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT seq), MIN(seq), MAX(seq)"
            " FROM events;").fetchone()
        assert tuple(row[index] for index in range(4)) == (96, 96, 1, 96)
    finally:
        conn.close()


@requires_pg
async def test_pg_task_manager_flow(pg_url, tmp_path):
    """TaskManager（create/delegate 前的状态面）在 PG 下工作。"""
    from orchestrator.task_manager import TaskManager

    tm = TaskManager(db_path=pg_url, workspace=tmp_path)
    tid = tm.create_task("pg task manager 验证", project="las")
    row = tm.conn.execute("SELECT status FROM tasks WHERE id = ?;",
                          (tid,)).fetchone()
    assert row["status"] == "queued"
    assert tm.review_result.__self__ is tm  # 对象可用
    tm.close()
