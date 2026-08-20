"""Structured Task Plan persistence and execution-time safety bindings."""

from __future__ import annotations

import json

import pytest

from hermes.action_policy import ActionPolicy
from hermes.policy import ApprovalPolicy
from hermes.tools import HermesTools
from orchestrator import (
    agent_profile_store,
    collaboration_store,
    state_store,
    task_plan_store,
)
from orchestrator.task_manager import TaskManager

pytestmark = pytest.mark.anyio


def _setup(tmp_path):
    tm = TaskManager(db_path=tmp_path / "state.db", workspace=tmp_path / "ws")
    agent_profile_store.seed_catalog(tm.conn)
    for name in ("codex", "kimi", "dsh"):
        state_store.update_heartbeat(
            tm.conn, name, endpoint=f"http://127.0.0.1/{name}", skills=[name])
        assert agent_profile_store.assign_seed_profile(tm.conn, name)
    conversation_id = collaboration_store.create_conversation(tm.conn)
    collaboration_id = collaboration_store.create_collaboration(
        tm.conn, conversation_id=conversation_id, objective="build and review")
    tools = HermesTools(
        tm, ApprovalPolicy(), collaboration_id=collaboration_id)
    return tm, tools, collaboration_id


def _steps():
    return [
        {
            "key": "backend",
            "objective": "implement backend",
            "agent_id": "codex",
            "depends_on": [],
            "expected_operations": ["filesystem.read", "filesystem.write"],
            "expected_artifacts": ["src/api.py", "tests/test_api.py"],
            "acceptance_criteria": ["tests pass"],
        },
        {
            "key": "review",
            "objective": "independent review",
            "agent_id": "dsh",
            "depends_on": ["backend"],
            "expected_operations": ["filesystem.read", "git.diff", "test.run"],
            "expected_artifacts": ["review.md"],
            "acceptance_criteria": ["risks and defects are reported"],
        },
    ]


async def test_create_task_plan_binds_tasks_agents_profiles_and_dependencies(
        tmp_path):
    tm, tools, collaboration_id = _setup(tmp_path)
    discovery = await tools.dispatch("list_agents", {})
    codex = next(item for item in discovery["agents"]
                 if item["id"] == "codex")
    assert codex["profile"]["version"] == 1
    assert "filesystem.write" in codex["profile"]["allowed_operations"]
    result = await tools.dispatch("create_task_plan", {
        "objective": "implement then review", "project": "agenthub",
        "steps": _steps(),
    })
    assert result["status"] == "active"
    assert result["revision"] == 1
    plan = task_plan_store.get_plan(tm.conn, result["plan_id"])
    assert plan["collaboration_id"] == collaboration_id
    rows = task_plan_store.list_steps(tm.conn, plan["id"])
    assert [row["step_key"] for row in rows] == ["backend", "review"]
    assert [row["profile_id"] for row in rows] == [
        "AP-CODEX-BACKEND", "AP-DSH-REVIEW"]
    backend_task = state_store.get_task(tm.conn, rows[0]["task_id"])
    review_task = state_store.get_task(tm.conn, rows[1]["task_id"])
    assert backend_task["plan_step_id"] == rows[0]["id"]
    context = json.loads(backend_task["plan_context_json"])
    assert context["profile_id"] == "AP-CODEX-BACKEND"
    assert context["acceptance_criteria"] == ["tests pass"]
    assert json.loads(review_task["depends_on_json"]) == [backend_task["id"]]
    assert backend_task["status"] == "queued"
    assert review_task["status"] == "created"


async def test_plan_rejects_agent_substitution_and_profile_drift(tmp_path):
    tm, tools, _ = _setup(tmp_path)
    result = await tools.dispatch("create_task_plan", {
        "objective": "implement then review", "steps": _steps(),
    })
    task_id = result["steps"][0]["task_id"]
    wrong = await tools.dispatch(
        "delegate_task", {"task_id": task_id, "agent_id": "kimi"})
    assert "绑定到 codex" in wrong["error"]

    profile = agent_profile_store.get_profile(tm.conn, "AP-CODEX-BACKEND")
    agent_profile_store.update_profile(
        tm.conn, "AP-CODEX-BACKEND", expected_version=profile["version"],
        updated_by="user", changes={"timeout_seconds": 7200})
    drift = await tools.dispatch(
        "delegate_task", {"task_id": task_id, "agent_id": "codex"})
    assert "重新规划" in drift["error"]


async def test_user_intervention_invalidates_existing_plan(tmp_path):
    tm, tools, collaboration_id = _setup(tmp_path)
    result = await tools.dispatch("create_task_plan", {
        "objective": "implement then review", "steps": _steps(),
    })
    collaboration_store.record_user_intervention(
        tm.conn, collaboration_id=collaboration_id, user_id="user",
        mode="steer", content={"text": "do not change the API"})
    rejected = await tools.dispatch("delegate_task", {
        "task_id": result["steps"][0]["task_id"], "agent_id": "codex",
    })
    assert "上下文已变化" in rejected["error"]


async def test_action_intent_outside_plan_escalates_to_user(tmp_path):
    tm, tools, collaboration_id = _setup(tmp_path)
    result = await tools.dispatch("create_task_plan", {
        "objective": "implement then review", "steps": _steps(),
    })
    task_id = result["steps"][0]["task_id"]
    intent = collaboration_store.request_action_intent(
        tm.conn, policy=ActionPolicy(workspace=tmp_path / "ws"),
        collaboration_id=collaboration_id, task_id=task_id,
        requested_by_agent_id="codex", operation="git.commit",
        targets=["."], purpose="commit work", expected_effects=["new commit"],
        rollback_plan="git reset HEAD^", based_on_revision=1)
    assert intent["status"] == "awaiting_user"
    assert "Task Plan" in intent["policy_reason"]


@pytest.mark.parametrize("steps", [
    [{"key": "a", "objective": "x", "agent_id": "codex",
      "depends_on": ["later"], "expected_operations": ["filesystem.read"],
      "acceptance_criteria": ["done"]}],
    [{"key": "a", "objective": "x", "agent_id": "codex",
      "expected_operations": [], "acceptance_criteria": ["done"]}],
])
async def test_invalid_plan_fails_before_creating_tasks(tmp_path, steps):
    tm, tools, _ = _setup(tmp_path)
    before = tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0]
    result = await tools.dispatch(
        "create_task_plan", {"objective": "bad", "steps": steps})
    assert "error" in result
    after = tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0]
    assert after == before


async def test_disabled_agent_requires_confirmation_before_plan_creation(
        tmp_path):
    tm, tools, _ = _setup(tmp_path)
    before = tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0]
    result = await tools.dispatch("create_task_plan", {
        "objective": "research with kimi",
        "steps": [{
            "key": "research",
            "objective": "research long context options",
            "agent_id": "kimi",
            "expected_operations": ["filesystem.read"],
            "acceptance_criteria": ["report risks"],
        }],
    })
    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "agent_disabled"
    assert result["agent_id"] == "kimi"
    assert tm.conn.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == before


async def test_disabled_agent_cannot_be_delegated_after_user_approval(tmp_path):
    tm, tools, _ = _setup(tmp_path)
    created = await tools.dispatch("create_task", {"objective": "research"})
    result = await tools.dispatch("approve_and_delegate", {
        "task_id": created["task_id"], "agent_id": "kimi",
        "note": "user asked for kimi",
    })
    assert result["status"] == "needs_confirmation"
    assert result["reason"] == "agent_disabled"
    task = state_store.get_task(tm.conn, created["task_id"])
    assert task["assigned_to"] is None
