"""Safety gates for operator-reviewed historical A2A task linkage."""

from __future__ import annotations

import json

import pytest

from common.models import TaskStatus
from orchestrator import collaboration_store, state_store
from orchestrator.a2a_backfill import (
    APPLY_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    apply_manifest,
    objective_sha256,
    plan_manifest,
    rollback_receipt,
)
from state.db import init_db, next_task_id


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "state.db")
    yield connection
    connection.close()


def _orphan(conn, *, status: str = TaskStatus.COMPLETED.value) -> tuple[dict, dict]:
    task_id = next_task_id(conn)
    state_store.create_task(
        conn,
        task_id=task_id,
        objective="复核 DSH 的完整历史结果",
        created_by="hermes",
        project="agentHub",
        assigned_to="dsh",
        status=status,
    )
    conn.execute(
        "UPDATE tasks SET result_summary = ? WHERE id = ?;",
        ("DSH review complete", task_id),
    )
    conn.commit()
    task = state_store.get_task(conn, task_id)
    state_store.record_event(conn, {
        "event_id": f"session-{task_id}",
        "event_type": "agent.session.event",
        "task_id": task_id,
        "source": "dsh",
        "payload": {
            "sessionId": "S-dsh-historical",
            "nativeSessionId": "native-dsh-historical",
            "adapterInstanceId": "dsh-old-instance",
        },
    })
    manifest = {
        "version": 1,
        "entries": [{
            "task_id": task_id,
            "peer": "qishuo",
            "context_id": "ctx-qishuo-historical",
            "agent": "dsh",
            "objective_sha256": objective_sha256(task["objective"]),
            "created_at": task["created_at"],
            "evidence": {"gateway_request_id": "req-reviewed"},
        }],
    }
    return manifest, {key: task[key] for key in task.keys()}


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]


def test_plan_is_read_only_and_exposes_exact_linkage(conn):
    manifest, task = _orphan(conn)
    before = {
        table: _count(conn, table) for table in (
            "conversations", "collaborations", "conversation_messages",
            "agent_session_bindings")
    }

    plan = plan_manifest(conn, manifest)

    assert plan["eligible"] is True
    assert plan["entries"][0]["task_id"] == task["id"]
    assert plan["entries"][0]["session"] == {
        "adapter_session_id": "S-dsh-historical",
        "native_session_id": "native-dsh-historical",
        "adapter_instance_id": "dsh-old-instance",
    }
    assert {table: _count(conn, table) for table in before} == before
    assert state_store.get_task(conn, task["id"])["collaboration_id"] is None


@pytest.mark.parametrize("field,value,error", [
    ("agent", "codex", "assigned agent mismatch"),
    ("objective_sha256", "0" * 64, "objective sha256 mismatch"),
    ("created_at", "2020-01-01T00:00:00+08:00", "created_at mismatch"),
])
def test_plan_rejects_evidence_mismatch(conn, field, value, error):
    manifest, _ = _orphan(conn)
    manifest["entries"][0][field] = value

    plan = plan_manifest(conn, manifest)

    assert plan["eligible"] is False
    assert error in plan["entries"][0]["errors"]
    assert _count(conn, "collaborations") == 0


def test_plan_rejects_nonterminal_task(conn):
    manifest, _ = _orphan(conn, status=TaskStatus.WORKING.value)

    plan = plan_manifest(conn, manifest)

    assert plan["eligible"] is False
    assert "task is not in a historical terminal state" in \
        plan["entries"][0]["errors"]


def test_apply_requires_confirmation_and_is_atomic_idempotent(conn):
    manifest, task = _orphan(conn)
    with pytest.raises(PermissionError):
        apply_manifest(conn, manifest, confirmation="yes")
    assert state_store.get_task(conn, task["id"])["collaboration_id"] is None

    receipt = apply_manifest(
        conn, manifest, confirmation=APPLY_CONFIRMATION)
    replay = apply_manifest(
        conn, manifest, confirmation=APPLY_CONFIRMATION)

    entry = receipt["entries"][0]
    assert replay == receipt
    assert state_store.get_task(conn, task["id"])["collaboration_id"] == \
        entry["collaboration_id"]
    messages = collaboration_store.list_collaboration_messages(
        conn, entry["collaboration_id"])
    assert [row["message_type"] for row in messages] == [
        "a2a.task.request.historical", "a2a.task.result.historical"]
    assert json.loads(messages[0]["content_json"])["text"] == task["objective"]
    binding = conn.execute(
        "SELECT * FROM agent_session_bindings WHERE id = ?;",
        (entry["binding_id"],),
    ).fetchone()
    assert binding["status"] == TaskStatus.COMPLETED.value
    assert binding["recovery_state"] == "historical_import"
    assert _count(conn, "conversation_messages") == 2
    assert _count(conn, "agent_session_bindings") == 1
    audit = conn.execute(
        "SELECT payload_json FROM events WHERE id = ?;",
        (f"a2a-backfill-{task['id']}",),
    ).fetchone()
    assert json.loads(audit["payload_json"])["receipt"] == entry


def test_apply_failure_rolls_back_entire_import(conn, monkeypatch):
    manifest, task = _orphan(conn)
    original = collaboration_store.append_message
    calls = 0

    def fail_on_result(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected historical result failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(collaboration_store, "append_message", fail_on_result)
    with pytest.raises(RuntimeError, match="injected"):
        apply_manifest(conn, manifest, confirmation=APPLY_CONFIRMATION)

    assert state_store.get_task(conn, task["id"])["collaboration_id"] is None
    assert _count(conn, "conversations") == 0
    assert _count(conn, "collaborations") == 0
    assert _count(conn, "conversation_messages") == 0
    assert _count(conn, "agent_session_bindings") == 0
    assert conn.execute(
        "SELECT 1 FROM events WHERE id = ?;",
        (f"a2a-backfill-{task['id']}",),
    ).fetchone() is None


def test_receipt_rollback_preserves_task_history_and_removes_only_import(conn):
    manifest, task = _orphan(conn)
    original_event_count = _count(conn, "events")
    receipt = apply_manifest(
        conn, manifest, confirmation=APPLY_CONFIRMATION)

    result = rollback_receipt(
        conn, receipt, confirmation=ROLLBACK_CONFIRMATION)

    assert result == {"rolled_back": [task["id"]]}
    restored = state_store.get_task(conn, task["id"])
    assert restored["collaboration_id"] is None
    assert restored["status"] == task["status"]
    assert restored["result_summary"] == task["result_summary"]
    assert _count(conn, "conversations") == 0
    assert _count(conn, "collaborations") == 0
    assert _count(conn, "conversation_messages") == 0
    assert _count(conn, "agent_session_bindings") == 0
    assert _count(conn, "events") > original_event_count
    assert conn.execute(
        "SELECT 1 FROM events WHERE id = ?;",
        (f"session-{task['id']}",),
    ).fetchone() is not None


def test_rollback_refuses_newer_collaboration_data_without_partial_change(conn):
    manifest, task = _orphan(conn)
    receipt = apply_manifest(
        conn, manifest, confirmation=APPLY_CONFIRMATION)
    entry = receipt["entries"][0]
    collaboration_store.append_message(
        conn,
        conversation_id=entry["conversation_id"],
        collaboration_id=entry["collaboration_id"],
        sender_type="user",
        sender_id="operator",
        content={"text": "newer message"},
        based_on_revision=1,
    )

    with pytest.raises(RuntimeError, match="has newer data"):
        rollback_receipt(
            conn, receipt, confirmation=ROLLBACK_CONFIRMATION)

    assert state_store.get_task(conn, task["id"])["collaboration_id"] == \
        entry["collaboration_id"]
    assert _count(conn, "conversation_messages") == 3


def test_rollback_rejects_unaudited_or_tampered_receipt(conn):
    manifest, _ = _orphan(conn)
    receipt = apply_manifest(
        conn, manifest, confirmation=APPLY_CONFIRMATION)
    tampered = json.loads(json.dumps(receipt))
    tampered["entries"][0]["message_ids"] = []

    with pytest.raises(ValueError, match="does not match audit"):
        rollback_receipt(
            conn, tampered, confirmation=ROLLBACK_CONFIRMATION)
    with pytest.raises(ValueError, match="entries must be non-empty"):
        rollback_receipt(
            conn,
            {"version": 1,
             "kind": "a2a-collaboration-backfill-receipt", "entries": []},
            confirmation=ROLLBACK_CONFIRMATION,
        )
