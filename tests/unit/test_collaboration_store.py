"""Persistent collaboration primitives from ADR-0004."""

from __future__ import annotations

import json

import pytest

from common.models import TaskStatus
from hermes.action_policy import ActionPolicy
from orchestrator import collaboration_store, state_store
from state.db import init_db, next_task_id


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "state.db")
    yield connection
    connection.close()


def _collaboration(conn):
    conversation_id = collaboration_store.create_conversation(
        conn, title="agentHub 开发", project="agentHub")
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id,
        objective="完成持久会话开发")
    return conversation_id, collaboration_id


def _task(conn, collaboration_id):
    task_id = next_task_id(conn)
    state_store.create_task(
        conn, task_id=task_id, objective="分析现有实现", created_by="hermes",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED)
    return task_id


def test_create_conversation_collaboration_and_task_link(conn):
    conversation_id, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)

    collaboration = collaboration_store.get_collaboration(conn, collaboration_id)
    assert collaboration["conversation_id"] == conversation_id
    assert collaboration["phase"] == "planning"
    assert collaboration["context_revision"] == 1
    assert state_store.get_task(conn, task_id)["collaboration_id"] == collaboration_id


@pytest.mark.parametrize(("statuses", "expected"), [
    (["queued"], "ready"),
    (["working", "awaiting_acceptance"], "executing"),
    (["awaiting_acceptance", "accepted"], "awaiting_acceptance"),
    (["accepted", "accepted"], "accepted"),
    (["cancelled", "cancelled"], "cancelled"),
    (["failed"], "needs_replan"),
])
def test_collaboration_phase_reducer_is_aggregate_and_order_independent(
        statuses, expected):
    assert collaboration_store.derive_phase_from_task_statuses(
        statuses).value == expected
    assert collaboration_store.derive_phase_from_task_statuses(
        reversed(statuses)).value == expected


def test_a2a_context_mapping_is_stable_and_peer_scoped(conn):
    first = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-shared",
        objective="first objective", project="agentHub")
    replay = collaboration_store.ensure_a2a_collaboration(
        conn, peer="qishuo", context_id="ctx-shared",
        objective="later objective", project="agentHub")
    other_peer = collaboration_store.ensure_a2a_collaboration(
        conn, peer="another-hermes", context_id="ctx-shared",
        objective="other peer objective", project="agentHub")

    assert replay == first
    assert other_peer["conversation_id"] != first["conversation_id"]
    assert other_peer["collaboration_id"] != first["collaboration_id"]
    assert conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM collaborations;").fetchone()[0] == 2


@pytest.mark.parametrize("peer,context_id", [
    ("", "ctx"),
    ("qishuo", ""),
    ("p" * 129, "ctx"),
    ("qishuo", "c" * 513),
])
def test_a2a_context_mapping_rejects_invalid_identity(conn, peer, context_id):
    with pytest.raises(ValueError):
        collaboration_store.ensure_a2a_collaboration(
            conn, peer=peer, context_id=context_id, objective="x")
    assert conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM collaborations;").fetchone()[0] == 0


def test_messages_are_ordered_and_idempotent(conn):
    conversation_id, collaboration_id = _collaboration(conn)
    first = collaboration_store.append_message(
        conn, conversation_id=conversation_id,
        collaboration_id=collaboration_id, sender_type="hermes",
        sender_id="hermes", content={"text": "请先给方案"},
        based_on_revision=1, idempotency_key="msg-1")
    replay = collaboration_store.append_message(
        conn, conversation_id=conversation_id,
        collaboration_id=collaboration_id, sender_type="hermes",
        sender_id="hermes", content={"text": "重复投递"},
        based_on_revision=1, idempotency_key="msg-1")
    second = collaboration_store.append_message(
        conn, conversation_id=conversation_id,
        collaboration_id=collaboration_id, sender_type="agent",
        sender_id="codex", agent_id="codex",
        content={"text": "方案如下"}, based_on_revision=1)

    assert replay["id"] == first["id"]
    assert [r["sequence"] for r in collaboration_store.list_messages(
        conn, conversation_id)] == [1, 2]
    assert json.loads(second["content_json"])["text"] == "方案如下"


def test_stale_agent_message_rejected_after_user_steer(conn):
    conversation_id, collaboration_id = _collaboration(conn)
    intervention = collaboration_store.record_user_intervention(
        conn, collaboration_id=collaboration_id, user_id="user",
        mode="steer", content={"text": "不要修改数据库"},
        idempotency_key="steer-1")

    collaboration = collaboration_store.get_collaboration(conn, collaboration_id)
    assert collaboration["context_revision"] == 2
    assert collaboration["phase"] == "needs_replan"
    assert collaboration["controller"] == "user"
    assert intervention["based_on_revision"] == 2

    with pytest.raises(collaboration_store.ContextConflict):
        collaboration_store.append_message(
            conn, conversation_id=conversation_id,
            collaboration_id=collaboration_id, sender_type="agent",
            sender_id="codex", content={"text": "按旧方案继续"},
            based_on_revision=1)


def test_message_to_hermes_does_not_interrupt_active_agent(conn):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    for status in (
        TaskStatus.ASSIGNED,
        TaskStatus.WORKING,
    ):
        state_store.transition_task(conn, task_id, status)
    collaboration_store.sync_phase_from_tasks(conn, collaboration_id)

    message = collaboration_store.append_user_message_to_hermes(
        conn,
        collaboration_id=collaboration_id,
        user_id="user",
        content={"text": "当前结果是什么意思？"},
    )

    collaboration = collaboration_store.get_collaboration(
        conn, collaboration_id)
    assert collaboration["context_revision"] == 1
    assert collaboration["phase"] == "executing"
    assert collaboration["controller"] == "hermes"
    assert message["based_on_revision"] == 1
    assert message["delivery_status"] == "queued"


def test_unknown_intervention_mode_rejected_without_revision_change(conn):
    _, collaboration_id = _collaboration(conn)
    with pytest.raises(ValueError, match="unsupported intervention mode"):
        collaboration_store.record_user_intervention(
            conn, collaboration_id=collaboration_id, user_id="user",
            mode="silently_override", content="bad mode")
    assert collaboration_store.get_collaboration(
        conn, collaboration_id)["context_revision"] == 1


def test_agent_session_replacement_keeps_history(conn):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    first = collaboration_store.bind_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", native_session_id="native-1",
        resume_capability="native")
    second = collaboration_store.bind_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", native_session_id="native-2",
        resume_capability="reconstructed",
        context_snapshot={"next": "continue tests"})

    assert first["id"] != second["id"]
    assert second["native_session_id"] == "native-2"
    assert second["context_revision"] == 1
    rows = conn.execute(
        "SELECT status, is_current FROM agent_session_bindings"
        " WHERE task_id = ? ORDER BY created_at, id;", (task_id,)).fetchall()
    assert sorted((r["status"], r["is_current"]) for r in rows) == [
        ("active", 1), ("replaced", 0)]


def test_agent_session_upsert_and_recovery_plan(conn):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    first = collaboration_store.upsert_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="adapter-1",
        native_session_id="native-1",
        capabilities={"native_resume": True, "durable_session": True},
        resume_capability="native", context_snapshot={"objective": "x"})
    replay = collaboration_store.upsert_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="adapter-1",
        native_session_id="native-1",
        capabilities={"native_resume": True, "durable_session": True},
        resume_capability="native", recovery_state="resumed")
    assert replay["id"] == first["id"]
    assert collaboration_store.session_recovery_plan(replay) == "native_resume"

    replacement = collaboration_store.upsert_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="codex", adapter_session_id="adapter-2",
        capabilities={"native_resume": False, "durable_session": False},
        resume_capability="snapshot", recovery_state="replaced",
        context_snapshot={"objective": "x"})
    assert replacement["id"] != first["id"]
    assert replacement["replacement_of_id"] == first["id"]
    assert collaboration_store.session_recovery_plan(replacement) == \
        "replacement"


def test_session_without_native_or_snapshot_is_blocked(conn):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    binding = collaboration_store.upsert_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="unknown", adapter_session_id="adapter-x",
        capabilities={})
    assert collaboration_store.session_recovery_plan(binding) == "blocked"


def test_session_interaction_is_idempotent_and_audited(conn):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    binding = collaboration_store.bind_agent_session(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        agent_id="dsh", native_session_id="native-dsh",
        adapter_session_id="adapter-dsh", resume_capability="native")
    native = {
        "interactionId": "dsh:rpc-1",
        "kind": "question",
        "nativeRequestId": "rpc-1",
        "payload": {"questions": [{"id": "q1", "question": "继续？"}]},
    }
    first = collaboration_store.upsert_session_interaction(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], agent_id="dsh",
        interaction=native)
    replay = collaboration_store.upsert_session_interaction(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        session_binding_id=binding["id"], agent_id="dsh",
        interaction=native)
    assert replay["id"] == first["id"]
    assert first["status"] == "pending"

    responding = collaboration_store.resolve_session_interaction(
        conn, first["id"], status="responding", resolved_by="user",
        response={"answer": {"answers": [{"id": "q1",
                                             "selected": ["继续"]}]}})
    assert responding["status"] == "responding"
    resolved = collaboration_store.resolve_session_interaction(
        conn, first["id"], status="resolved", resolved_by="user",
        response={"answer": {"answers": [{"id": "q1",
                                             "selected": ["继续"]}]}})
    assert resolved["resolved_at"]
    assert [row["event_type"] for row in conn.execute(
        "SELECT event_type FROM events WHERE task_id = ? ORDER BY seq;",
        (task_id,)).fetchall()] == [
            "agent.session.bound", "agent.interaction.requested",
            "agent.interaction.responding", "agent.interaction.resolved"]


def test_action_intent_authority_and_revision(conn, tmp_path):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    intent = collaboration_store.create_action_intent(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        requested_by_agent_id="codex", operation="filesystem.write",
        targets=["src/api.py"], purpose="实现接口",
        expected_effects=["修改源文件"], rollback_plan="git restore",
        risk="write", based_on_revision=1)

    routed = collaboration_store.route_action_intent(
        conn, intent["id"], policy=ActionPolicy(workspace=tmp_path))
    assert routed["status"] == "awaiting_hermes"

    with pytest.raises(PermissionError):
        collaboration_store.decide_action_intent(
            conn, intent["id"], approved=True, decided_by="codex")

    collaboration_store.record_user_intervention(
        conn, collaboration_id=collaboration_id, user_id="user",
        mode="steer", content="改变实现方向")
    with pytest.raises(collaboration_store.ContextConflict):
        collaboration_store.decide_action_intent(
            conn, intent["id"], approved=True, decided_by="hermes")

    rejected = collaboration_store.decide_action_intent(
        conn, intent["id"], approved=False, decided_by="user",
        note="旧 revision 已失效")
    assert rejected["status"] == "rejected"


def test_action_intent_uses_task_execution_workspace(conn, tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "agenthub"))
    _, collaboration_id = _collaboration(conn)
    execution_workspace = tmp_path / "project"
    execution_workspace.mkdir()
    task_id = next_task_id(conn)
    state_store.create_task(
        conn, task_id=task_id, objective="修改项目", created_by="hermes",
        collaboration_id=collaboration_id, status=TaskStatus.QUEUED,
        plan_context={"execution_workspace": str(execution_workspace)},
    )
    intent = collaboration_store.create_action_intent(
        conn, collaboration_id=collaboration_id, task_id=task_id,
        requested_by_agent_id="dsh", operation="filesystem.write",
        targets=[str(execution_workspace / "src" / "app.py")],
        purpose="修复缺陷", expected_effects=["修改源文件"],
        rollback_plan="git restore src/app.py", risk="write",
        based_on_revision=1,
    )

    routed = collaboration_store.route_action_intent(conn, intent["id"])

    assert routed["status"] == "awaiting_hermes"
    assert routed["policy_route"] == "hermes"


def test_action_intent_routes_and_audits(conn, tmp_path):
    _, collaboration_id = _collaboration(conn)
    task_id = _task(conn, collaboration_id)
    policy = ActionPolicy(workspace=tmp_path)

    read_intent = collaboration_store.request_action_intent(
        conn, policy=policy, collaboration_id=collaboration_id,
        task_id=task_id, requested_by_agent_id="codex",
        operation="filesystem.read", targets=["src/api.py"],
        purpose="读取实现", expected_effects=[], based_on_revision=1)
    assert read_intent["status"] == "approved"
    assert read_intent["decided_by"] == "policy"

    critical = collaboration_store.request_action_intent(
        conn, policy=policy, collaboration_id=collaboration_id,
        task_id=task_id, requested_by_agent_id="codex",
        operation="git.push", targets=["."], purpose="发布代码",
        expected_effects=["更新远端"], rollback_plan="git revert",
        based_on_revision=1)
    assert critical["status"] == "awaiting_user"
    with pytest.raises(PermissionError, match="requires user approval"):
        collaboration_store.decide_action_intent(
            conn, critical["id"], approved=True, decided_by="hermes")
    approved = collaboration_store.decide_action_intent(
        conn, critical["id"], approved=True, decided_by="user")
    assert approved["status"] == "approved"

    event_types = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events WHERE task_id = ? ORDER BY seq;",
        (task_id,)).fetchall()]
    assert event_types == [
        "action.intent.created", "action.intent.routed",
        "action.intent.created", "action.intent.routed",
        "action.intent.approved",
    ]


def test_message_and_user_intervention_events(conn):
    conversation_id, collaboration_id = _collaboration(conn)
    collaboration_store.append_message(
        conn, conversation_id=conversation_id,
        collaboration_id=collaboration_id, sender_type="hermes",
        sender_id="hermes", content="hello", based_on_revision=1)
    collaboration_store.record_user_intervention(
        conn, collaboration_id=collaboration_id, user_id="user",
        mode="pause", content="pause now")

    event_types = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events ORDER BY seq;").fetchall()]
    assert event_types == [
        "conversation.message.created",
        "conversation.message.created",
        "user.intervened",
    ]
