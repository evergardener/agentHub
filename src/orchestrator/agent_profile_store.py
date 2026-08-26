"""Versioned Agent Template/Profile persistence and assignment."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import yaml

from state.db import now_iso


JSON_FIELDS = {
    "responsibilities", "allowed_operations", "denied_operations",
    "allowed_tools", "workspace_roots", "task_types", "cost_limit",
    "allowed_models", "allowed_reasoning_efforts",
}
PROFILE_FIELDS = {
    "name", "project", "role_prompt", "responsibilities",
    "execution_mode", "allowed_operations", "denied_operations",
    "allowed_tools", "workspace_roots", "task_types",
    "reviewer_profile_id", "model", "allowed_models",
    "reasoning_effort", "allowed_reasoning_efforts", "cost_limit", "priority",
    "timeout_seconds", "max_concurrent_tasks", "approval_level", "status",
}
DEFAULT_CATALOG = (
    Path(__file__).resolve().parents[2] / "config" / "agent_templates.yaml"
)


class ProfileVersionConflict(RuntimeError):
    pass


def _validate_runtime_policy(
    *, model: str | None, allowed_models: list[str] | None,
    reasoning_effort: str | None,
    allowed_reasoning_efforts: list[str] | None,
) -> None:
    from orchestrator.runtime_config import normalize_runtime_config

    for field, values in (
            ("allowed_models", allowed_models),
            ("allowed_reasoning_efforts", allowed_reasoning_efforts)):
        if values is not None and (
                not isinstance(values, list)
                or any(not isinstance(item, str) or not item.strip()
                       for item in values)
                or len(values) != len(set(values))):
            raise ValueError(f"{field} must be a unique list of identifiers")
    normalize_runtime_config(model, reasoning_effort)
    for item in allowed_models or []:
        normalize_runtime_config(item, None)
    for item in allowed_reasoning_efforts or []:
        normalize_runtime_config(None, item)
    if model is not None and model not in set(allowed_models or []):
        raise ValueError("model must be present in allowed_models")
    if (reasoning_effort is not None
            and reasoning_effort not in set(allowed_reasoning_efforts or [])):
        raise ValueError(
            "reasoning_effort must be present in allowed_reasoning_efforts")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _profile_snapshot(row) -> dict:
    snapshot = _row_dict(row)
    for field in JSON_FIELDS:
        key = f"{field}_json"
        if key in snapshot:
            snapshot[field] = json.loads(snapshot.pop(key) or "null")
    return snapshot


def _audit(conn, event_type: str, *, source: str, payload: dict) -> None:
    from orchestrator import state_store

    state_store.record_event(conn, {
        "event_id": _id("E"),
        "event_type": event_type,
        "source": source,
        "payload": payload,
    }, commit=False)


def create_template(conn, *, template_id: str, name: str,
                    adapter_kind: str, description: str | None = None,
                    capabilities: dict | None = None,
                    default_config: dict | None = None,
                    enabled: bool = True) -> str:
    ts = now_iso()
    conn.execute(
        "INSERT INTO agent_templates (id, name, adapter_kind, description,"
        " capabilities_json, default_config_json, enabled, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?);",
        (template_id, name, adapter_kind, description,
         _json(capabilities or {}), _json(default_config or {}),
         int(enabled), ts, ts),
    )
    conn.commit()
    return template_id


def get_template(conn, template_id: str):
    return conn.execute(
        "SELECT * FROM agent_templates WHERE id = ?;", (template_id,)
    ).fetchone()


def get_profile(conn, profile_id: str):
    return conn.execute(
        "SELECT * FROM agent_profiles WHERE id = ?;", (profile_id,)
    ).fetchone()


def profile_policy(conn, profile_id: str) -> dict:
    row = get_profile(conn, profile_id)
    if row is None:
        raise KeyError(f"agent profile not found: {profile_id}")
    return _profile_snapshot(row)


def _insert_version(conn, row, *, created_by: str) -> None:
    snapshot = _profile_snapshot(row)
    conn.execute(
        "INSERT INTO agent_profile_versions (id, profile_id, version,"
        " snapshot_json, created_by, created_at) VALUES (?,?,?,?,?,?);",
        (_id("APV"), row["id"], row["version"], _json(snapshot),
         created_by, now_iso()),
    )


def create_profile(conn, *, template_id: str, name: str, created_by: str,
                   profile_id: str | None = None, project: str | None = None,
                   role_prompt: str | None = None,
                   responsibilities: list[str] | None = None,
                   execution_mode: str = "read_only",
                   allowed_operations: list[str] | None = None,
                   denied_operations: list[str] | None = None,
                   allowed_tools: list[str] | None = None,
                   workspace_roots: list[str] | None = None,
                   task_types: list[str] | None = None,
                   reviewer_profile_id: str | None = None,
                   model: str | None = None,
                   allowed_models: list[str] | None = None,
                   reasoning_effort: str | None = None,
                   allowed_reasoning_efforts: list[str] | None = None,
                   cost_limit: dict | None = None,
                   priority: int = 50, timeout_seconds: int = 3600,
                   max_concurrent_tasks: int = 1,
                   approval_level: str = "hermes",
                   status: str = "active") -> str:
    if get_template(conn, template_id) is None:
        raise KeyError(f"agent template not found: {template_id}")
    if execution_mode not in {"read_only", "execute"}:
        raise ValueError("execution_mode must be read_only or execute")
    _validate_runtime_policy(
        model=model, allowed_models=allowed_models,
        reasoning_effort=reasoning_effort,
        allowed_reasoning_efforts=allowed_reasoning_efforts,
    )
    profile_id = profile_id or _id("AP")
    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO agent_profiles (id, template_id, name, project,"
            " role_prompt, responsibilities_json, execution_mode,"
            " allowed_operations_json, denied_operations_json,"
            " allowed_tools_json, workspace_roots_json, task_types_json,"
            " reviewer_profile_id, model, allowed_models_json,"
            " reasoning_effort, allowed_reasoning_efforts_json,"
            " cost_limit_json, priority,"
            " timeout_seconds, max_concurrent_tasks, approval_level, status,"
            " version, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?);",
            (profile_id, template_id, name, project, role_prompt,
             _json(responsibilities or []), execution_mode,
             _json(allowed_operations or []), _json(denied_operations or []),
             _json(allowed_tools or []), _json(workspace_roots or []),
             _json(task_types or []), reviewer_profile_id, model,
             _json(allowed_models or []), reasoning_effort,
             _json(allowed_reasoning_efforts or []),
             _json(cost_limit) if cost_limit is not None else None,
             priority, timeout_seconds, max_concurrent_tasks, approval_level,
             status, created_by, ts, ts),
        )
        row = get_profile(conn, profile_id)
        _insert_version(conn, row, created_by=created_by)
        _audit(conn, "agent.profile.created", source=created_by,
               payload={"profile_id": profile_id, "version": 1})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return profile_id


def update_profile(conn, profile_id: str, *, expected_version: int,
                   updated_by: str, changes: dict[str, Any]):
    unknown = set(changes) - PROFILE_FIELDS
    if unknown:
        raise ValueError(f"unsupported profile fields: {sorted(unknown)}")
    if "execution_mode" in changes and changes["execution_mode"] not in {
        "read_only", "execute"
    }:
        raise ValueError("execution_mode must be read_only or execute")
    row = get_profile(conn, profile_id)
    if row is None:
        raise KeyError(f"agent profile not found: {profile_id}")
    if row["version"] != expected_version:
        raise ProfileVersionConflict(
            f"profile {profile_id} expected version {expected_version}, "
            f"current {row['version']}")
    current = _profile_snapshot(row)
    _validate_runtime_policy(
        model=changes.get("model", current.get("model")),
        allowed_models=changes.get(
            "allowed_models", current.get("allowed_models")),
        reasoning_effort=changes.get(
            "reasoning_effort", current.get("reasoning_effort")),
        allowed_reasoning_efforts=changes.get(
            "allowed_reasoning_efforts",
            current.get("allowed_reasoning_efforts")),
    )
    if not changes:
        return row

    assignments: list[str] = []
    values: list[Any] = []
    for field, value in changes.items():
        column = f"{field}_json" if field in JSON_FIELDS else field
        assignments.append(f"{column} = ?")
        values.append(_json(value) if field in JSON_FIELDS and value is not None
                      else value)
    assignments.extend(["version = version + 1", "updated_at = ?"])
    values.extend([now_iso(), profile_id, expected_version])
    try:
        cur = conn.execute(
            f"UPDATE agent_profiles SET {', '.join(assignments)}"
            " WHERE id = ? AND version = ?;", values)
        if cur.rowcount != 1:
            raise ProfileVersionConflict(
                f"profile {profile_id} changed concurrently")
        row = get_profile(conn, profile_id)
        _insert_version(conn, row, created_by=updated_by)
        _audit(conn, "agent.profile.updated", source=updated_by,
               payload={"profile_id": profile_id,
                        "version": row["version"],
                        "changed_fields": sorted(changes)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row


def rollback_profile(conn, profile_id: str, *, target_version: int,
                     expected_version: int, updated_by: str):
    version = conn.execute(
        "SELECT snapshot_json FROM agent_profile_versions"
        " WHERE profile_id = ? AND version = ?;",
        (profile_id, target_version),
    ).fetchone()
    if version is None:
        raise KeyError(
            f"profile version not found: {profile_id}@{target_version}")
    snapshot = json.loads(version["snapshot_json"])
    changes = {field: snapshot.get(field) for field in PROFILE_FIELDS
               if field in snapshot}
    return update_profile(
        conn, profile_id, expected_version=expected_version,
        updated_by=updated_by, changes=changes)


def list_profile_versions(conn, profile_id: str):
    return conn.execute(
        "SELECT * FROM agent_profile_versions WHERE profile_id = ?"
        " ORDER BY version;", (profile_id,)
    ).fetchall()


def assign_agent_profile(conn, *, agent_id: str, template_id: str,
                         profile_id: str, assigned_by: str) -> None:
    profile = get_profile(conn, profile_id)
    if profile is None:
        raise KeyError(f"agent profile not found: {profile_id}")
    if profile["template_id"] != template_id:
        raise ValueError("profile does not belong to template")
    try:
        cur = conn.execute(
            "UPDATE agents SET template_id = ?, profile_id = ?,"
            " updated_at = ? WHERE id = ?;",
            (template_id, profile_id, now_iso(), agent_id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"agent not found: {agent_id}")
        _audit(conn, "agent.profile.assigned", source=assigned_by,
               payload={"agent_id": agent_id, "template_id": template_id,
                        "profile_id": profile_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def seed_catalog(conn, config_path: str | Path = DEFAULT_CATALOG,
                 *, created_by: str = "system:catalog-seed") -> dict[str, list[str]]:
    """Idempotently create configured templates and initial profiles.

    Existing rows are never overwritten: WebUI/user edits remain authoritative.
    New defaults can therefore be added by a later release without resetting
    an operator's versioned profile history.
    """
    path = Path(config_path)
    if not path.exists():
        return {"templates": [], "profiles": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    created: dict[str, list[str]] = {"templates": [], "profiles": []}
    for spec in data.get("templates") or []:
        template_id = str(spec["id"])
        if get_template(conn, template_id) is not None:
            continue
        create_template(
            conn,
            template_id=template_id,
            name=str(spec["name"]),
            adapter_kind=str(spec["adapter_kind"]),
            description=spec.get("description"),
            capabilities=spec.get("capabilities"),
            default_config=spec.get("default_config"),
            enabled=bool(spec.get("enabled", True)),
        )
        created["templates"].append(template_id)
    for spec in data.get("profiles") or []:
        profile_id = str(spec["id"])
        if get_profile(conn, profile_id) is not None:
            continue
        values = {key: value for key, value in spec.items()
                  if key not in {"id", "template_id", "name"}}
        create_profile(
            conn,
            template_id=str(spec["template_id"]),
            profile_id=profile_id,
            name=str(spec["name"]),
            created_by=created_by,
            **values,
        )
        created["profiles"].append(profile_id)
    _apply_seed_profile_upgrades(conn, data, updated_by=created_by)
    return created


def _apply_seed_profile_upgrades(conn, data: dict, *, updated_by: str) -> None:
    """Apply additive upgrades only to untouched, versioned seed profiles.

    Catalog defaults are normally create-only so operator changes remain
    authoritative. Security capabilities occasionally need an explicit
    rollout to already-installed stock profiles. Such upgrades must name the
    exact source version, preserve every existing field, and only add values.
    Any profile changed through the versioned API no longer matches and is
    left untouched.
    """
    for spec in data.get("profile_upgrades") or []:
        if not isinstance(spec, dict):
            continue
        profile_id = str(spec.get("profile_id") or "")
        from_version = spec.get("from_version")
        if not profile_id or not isinstance(from_version, int):
            continue
        row = get_profile(conn, profile_id)
        if (row is None or row["version"] != from_version
                or row["created_by"] != "system:catalog-seed"):
            continue
        current = _profile_snapshot(row)
        changes: dict[str, list[str]] = {}
        for field, additions_key in (
                ("allowed_operations", "add_allowed_operations"),
                ("allowed_tools", "add_allowed_tools")):
            additions = spec.get(additions_key) or []
            if (not isinstance(additions, list)
                    or any(not isinstance(item, str) or not item
                           for item in additions)):
                raise ValueError(
                    f"invalid catalog profile upgrade {profile_id}: "
                    f"{additions_key}")
            values = list(current.get(field) or [])
            merged = values + [item for item in additions if item not in values]
            if merged != values:
                changes[field] = merged
        if changes:
            update_profile(
                conn, profile_id, expected_version=from_version,
                updated_by=updated_by, changes=changes)


def assign_seed_profile(conn, agent_id: str,
                        config_path: str | Path = DEFAULT_CATALOG,
                        *, assigned_by: str = "system:catalog-seed") -> bool:
    """Assign a configured initial profile only to an unassigned agent."""
    agent = conn.execute(
        "SELECT template_id, profile_id FROM agents WHERE id = ?;",
        (agent_id,),
    ).fetchone()
    if agent is None or agent["profile_id"] is not None:
        return False
    path = Path(config_path)
    if not path.exists():
        return False
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    spec = (data.get("assignments") or {}).get(agent_id)
    if not isinstance(spec, dict):
        return False
    assign_agent_profile(
        conn,
        agent_id=agent_id,
        template_id=str(spec["template_id"]),
        profile_id=str(spec["profile_id"]),
        assigned_by=assigned_by,
    )
    return True
