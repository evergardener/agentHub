"""Versioned Agent Profile persistence and policy-bound routing tests."""

from __future__ import annotations

import json

import pytest

from state.db import init_db


@pytest.fixture
def conn(tmp_path):
    return init_db(tmp_path / "state.db")


def _template(conn):
    from orchestrator import agent_profile_store

    return agent_profile_store.create_template(
        conn, template_id="template-codex", name="Codex",
        adapter_kind="codex-cli",
        capabilities={"multi_turn": False, "native_resume": False})


def test_profile_is_versioned_audited_and_rollbackable(conn):
    from orchestrator import agent_profile_store

    template_id = _template(conn)
    profile_id = agent_profile_store.create_profile(
        conn, template_id=template_id, name="backend", created_by="user-1",
        role_prompt="implement backend", responsibilities=["backend"],
        execution_mode="execute",
        allowed_operations=["filesystem.read", "filesystem.write"],
        workspace_roots=["src"], status="active")

    first = agent_profile_store.profile_policy(conn, profile_id)
    assert first["version"] == 1
    assert first["allowed_operations"] == [
        "filesystem.read", "filesystem.write"]

    second = agent_profile_store.update_profile(
        conn, profile_id, expected_version=1, updated_by="user-1",
        changes={"name": "backend-v2", "timeout_seconds": 7200})
    assert second["version"] == 2
    with pytest.raises(agent_profile_store.ProfileVersionConflict):
        agent_profile_store.update_profile(
            conn, profile_id, expected_version=1, updated_by="user-1",
            changes={"name": "stale"})

    restored = agent_profile_store.rollback_profile(
        conn, profile_id, target_version=1, expected_version=2,
        updated_by="user-1")
    assert restored["version"] == 3
    assert restored["name"] == "backend"
    assert [r["version"] for r in agent_profile_store.list_profile_versions(
        conn, profile_id)] == [1, 2, 3]

    event_types = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM events ORDER BY seq;").fetchall()]
    assert event_types == [
        "agent.profile.created", "agent.profile.updated",
        "agent.profile.updated"]


def test_profile_assignment_and_action_intent_restriction(conn, tmp_path,
                                                          monkeypatch):
    from hermes.action_policy import ActionPolicy
    from orchestrator import agent_profile_store, collaboration_store, state_store

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    template_id = _template(conn)
    profile_id = agent_profile_store.create_profile(
        conn, template_id=template_id, name="review-only",
        created_by="user-1", execution_mode="read_only",
        allowed_operations=["filesystem.read", "filesystem.write"])
    state_store.update_heartbeat(
        conn, "codex", endpoint="http://codex", skills=["coding"])
    agent_profile_store.assign_agent_profile(
        conn, agent_id="codex", template_id=template_id,
        profile_id=profile_id, assigned_by="user-1")

    assigned = conn.execute(
        "SELECT template_id, profile_id FROM agents WHERE id = 'codex';"
    ).fetchone()
    assert assigned["profile_id"] == profile_id

    conversation_id = collaboration_store.create_conversation(conn)
    collaboration_id = collaboration_store.create_collaboration(
        conn, conversation_id=conversation_id, objective="profile policy")
    routed = collaboration_store.request_action_intent(
        conn, policy=ActionPolicy(workspace=workspace),
        collaboration_id=collaboration_id, task_id="T-profile",
        requested_by_agent_id="codex", operation="filesystem.write",
        targets=["src/app.py"], purpose="implement",
        expected_effects=["changes app"], rollback_plan="git restore src/app.py",
        based_on_revision=1)
    assert routed["status"] == "awaiting_user"
    assert "只读" in routed["policy_reason"]

    audit = conn.execute(
        "SELECT payload_json FROM events WHERE event_type ="
        " 'action.intent.routed' ORDER BY seq DESC LIMIT 1;"
    ).fetchone()
    assert json.loads(audit["payload_json"])["profile_id"] == profile_id


def test_profile_cannot_lower_global_user_approval(tmp_path):
    from hermes.action_policy import ActionPolicy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation="git.push", targets=["."], rollback_plan="revert",
        profile={
            "status": "active", "execution_mode": "execute",
            "allowed_operations": ["git.push"], "approval_level": "hermes",
        })
    assert decision.route == "user"
    assert "策略要求用户批准" in decision.reason


def test_dsh_catalog_seed_is_idempotent_and_assigns_only_when_empty(conn):
    from orchestrator import agent_profile_store, state_store

    first = agent_profile_store.seed_catalog(conn)
    second = agent_profile_store.seed_catalog(conn)
    assert first == {
        "templates": ["TPL-CODEX", "TPL-KIMI", "TPL-DSH"],
        "profiles": [
            "AP-CODEX-BACKEND", "AP-KIMI-FRONTEND", "AP-DSH-REVIEW"],
    }
    assert second == {"templates": [], "profiles": []}
    profile = agent_profile_store.profile_policy(conn, "AP-DSH-REVIEW")
    assert profile["execution_mode"] == "read_only"
    assert "filesystem.write" in profile["denied_operations"]

    state_store.update_heartbeat(
        conn, "dsh", endpoint="http://dsh:8203", skills=["code_review"])
    assert agent_profile_store.assign_seed_profile(conn, "dsh") is True
    assigned = conn.execute(
        "SELECT template_id, profile_id FROM agents WHERE id = 'dsh';"
    ).fetchone()
    assert assigned["template_id"] == "TPL-DSH"
    assert assigned["profile_id"] == "AP-DSH-REVIEW"
    assert agent_profile_store.assign_seed_profile(conn, "dsh") is False
