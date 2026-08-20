"""Versioned structured Task Plans and immutable Agent/Profile bindings."""

from __future__ import annotations

import json
import re
import uuid

from state.db import now_iso

_STEP_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_steps(steps: list[dict]) -> None:
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise ValueError("Task Plan 必须包含 1..50 个步骤")
    seen: set[str] = set()
    for step in steps:
        key = step.get("key")
        if not isinstance(key, str) or not _STEP_KEY.fullmatch(key):
            raise ValueError("step key 必须是 1..64 位字母数字或 _/-")
        if key in seen:
            raise ValueError(f"重复 step key: {key}")
        objective = step.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError(f"步骤 {key} 缺少 objective")
        criteria = step.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or not all(
                isinstance(item, str) and item.strip() for item in criteria):
            raise ValueError(f"步骤 {key} 必须提供非空 acceptance_criteria")
        operations = step.get("expected_operations")
        if not isinstance(operations, list) or not operations or not all(
                isinstance(item, str) and item.strip() for item in operations):
            raise ValueError(f"步骤 {key} 必须提供 expected_operations")
        deps = step.get("depends_on") or []
        if not isinstance(deps, list) or any(dep not in seen for dep in deps):
            raise ValueError(f"步骤 {key} 只能依赖此前已声明的 step key")
        seen.add(key)


def create_plan(conn, *, collaboration_id: str, objective: str,
                project: str | None, steps: list[dict],
                created_by: str = "hermes"):
    validate_steps(steps)
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("Task Plan objective 不能为空")
    collaboration = conn.execute(
        "SELECT context_revision FROM collaborations WHERE id = ?;",
        (collaboration_id,),
    ).fetchone()
    if collaboration is None:
        raise KeyError(f"collaboration not found: {collaboration_id}")
    for step in steps:
        task = conn.execute(
            "SELECT collaboration_id, plan_step_id FROM tasks WHERE id = ?;",
            (step.get("task_id"),),
        ).fetchone()
        profile = conn.execute(
            "SELECT version, status FROM agent_profiles WHERE id = ?;",
            (step.get("profile_id"),),
        ).fetchone()
        agent = conn.execute(
            "SELECT profile_id FROM agents WHERE id = ?;",
            (step.get("agent_id"),),
        ).fetchone()
        if (task is None or task["collaboration_id"] != collaboration_id
                or task["plan_step_id"] is not None):
            raise ValueError(
                f"步骤 {step['key']} 的 task 不属于当前 collaboration 或已绑定")
        if (profile is None or profile["status"] != "active"
                or profile["version"] != step.get("profile_version")
                or agent is None or agent["profile_id"] != step.get("profile_id")):
            raise PermissionError(
                f"步骤 {step['key']} 的 Agent/Profile 快照无效")
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM task_plans"
        " WHERE collaboration_id = ?;", (collaboration_id,),
    ).fetchone()
    revision = row[0] + 1
    plan_id = _id("PLAN")
    now = now_iso()
    try:
        conn.execute(
            "UPDATE task_plans SET status = 'superseded', updated_at = ?"
            " WHERE collaboration_id = ? AND status = 'active';",
            (now, collaboration_id),
        )
        conn.execute(
            "INSERT INTO task_plans (id, collaboration_id, revision, objective,"
            " project, status, based_on_revision, created_by, created_at,"
            " updated_at) VALUES (?,?,?,?,?,'active',?,?,?,?);",
            (plan_id, collaboration_id, revision, objective, project,
             collaboration["context_revision"], created_by, now, now),
        )
        for ordinal, step in enumerate(steps, 1):
            step_id = _id("STEP")
            conn.execute(
                "INSERT INTO task_plan_steps (id, plan_id, step_key, ordinal,"
                " task_id, objective, agent_id, profile_id, profile_version,"
                " depends_on_json, expected_artifacts_json,"
                " acceptance_criteria_json, expected_operations_json,"
                " status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'planned',?);",
                (step_id, plan_id, step["key"], ordinal, step["task_id"],
                 step["objective"], step["agent_id"], step["profile_id"],
                 step["profile_version"], _json(step.get("depends_on") or []),
                 _json(step.get("expected_artifacts") or []),
                 _json(step["acceptance_criteria"]),
                 _json(step["expected_operations"]), now),
            )
            conn.execute(
                "UPDATE tasks SET plan_step_id = ? WHERE id = ?;",
                (step_id, step["task_id"]),
            )
        from orchestrator import state_store

        state_store.record_event(conn, {
            "event_id": _id("E"), "event_type": "task.plan.activated",
            "source": created_by,
            "payload": {"plan_id": plan_id,
                        "collaboration_id": collaboration_id,
                        "revision": revision, "step_count": len(steps)},
        }, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_plan(conn, plan_id)


def get_plan(conn, plan_id: str):
    return conn.execute(
        "SELECT * FROM task_plans WHERE id = ?;", (plan_id,)
    ).fetchone()


def list_steps(conn, plan_id: str):
    return conn.execute(
        "SELECT * FROM task_plan_steps WHERE plan_id = ? ORDER BY ordinal;",
        (plan_id,),
    ).fetchall()


def get_step_for_task(conn, task_id: str):
    return conn.execute(
        "SELECT s.*, p.status AS plan_status,"
        " p.based_on_revision AS plan_context_revision,"
        " p.collaboration_id AS plan_collaboration_id"
        " FROM task_plan_steps s"
        " JOIN task_plans p ON p.id = s.plan_id WHERE s.task_id = ?;",
        (task_id,),
    ).fetchone()


def validate_delegation(conn, *, task_id: str, agent_id: str) -> None:
    step = get_step_for_task(conn, task_id)
    if step is None:
        return
    if step["plan_status"] != "active":
        raise ValueError("Task Plan 已失效，必须重新规划")
    collaboration = conn.execute(
        "SELECT context_revision FROM collaborations WHERE id = ?;",
        (step["plan_collaboration_id"],),
    ).fetchone()
    if (collaboration is None
            or collaboration["context_revision"] != step["plan_context_revision"]):
        raise ValueError("协作上下文已变化，必须重新规划")
    if step["agent_id"] != agent_id:
        raise PermissionError(
            f"Task Plan 将该步骤绑定到 {step['agent_id']}，不能委派给 {agent_id}")
    agent = conn.execute(
        "SELECT profile_id FROM agents WHERE id = ?;", (agent_id,)
    ).fetchone()
    profile = conn.execute(
        "SELECT version, status FROM agent_profiles WHERE id = ?;",
        (step["profile_id"],),
    ).fetchone()
    if (agent is None or agent["profile_id"] != step["profile_id"]
            or profile is None or profile["status"] != "active"
            or profile["version"] != step["profile_version"]):
        raise PermissionError("Agent/Profile 已变化，必须重新规划后再委派")
