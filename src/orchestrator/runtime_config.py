"""Fail-closed task-level model and reasoning configuration."""

from __future__ import annotations

import re
from typing import Any


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_EFFORT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SUPPORTED_ADAPTERS = frozenset({"codex-app-server", "codex-cli"})


def normalize_runtime_config(
    model: Any = None, reasoning_effort: Any = None,
) -> dict[str, str] | None:
    """Normalize external runtime overrides without guessing aliases."""
    if model is None and reasoning_effort is None:
        return None
    result: dict[str, str] = {}
    if model is not None:
        if not isinstance(model, str) or not _MODEL.fullmatch(model.strip()):
            raise ValueError(
                "model must be a 1-128 character model identifier")
        result["model"] = model.strip()
    if reasoning_effort is not None:
        if (not isinstance(reasoning_effort, str)
                or not _EFFORT.fullmatch(reasoning_effort.strip())):
            raise ValueError(
                "reasoning_effort must be a lowercase runtime identifier")
        result["reasoning_effort"] = reasoning_effort.strip()
    return result


def resolve_runtime_config(
    conn, *, agent_id: str, requested: dict[str, Any] | None,
    include_profile_defaults: bool = False,
) -> dict[str, str] | None:
    """Bind overrides to the selected Agent Profile and Adapter.

    An explicit override is rejected when the Agent has no active versioned
    Profile, the Adapter cannot enforce it, or the Profile allowlist is empty.
    """
    requested = normalize_runtime_config(
        (requested or {}).get("model"),
        (requested or {}).get("reasoning_effort"),
    )
    row = conn.execute(
        "SELECT a.profile_id, p.status, p.model, p.allowed_models_json,"
        " p.reasoning_effort, p.allowed_reasoning_efforts_json,"
        " t.adapter_kind FROM agents a"
        " LEFT JOIN agent_profiles p ON p.id = a.profile_id"
        " LEFT JOIN agent_templates t ON t.id = a.template_id"
        " WHERE a.id = ?;",
        (agent_id,),
    ).fetchone()
    if requested is None and not include_profile_defaults:
        return None
    if row is None or not row["profile_id"] or row["status"] != "active":
        if requested is None:
            return None
        raise PermissionError(
            f"agent {agent_id} has no active Agent Profile for runtime config")
    if row["adapter_kind"] not in _SUPPORTED_ADAPTERS:
        if requested is None:
            return None
        raise ValueError(
            f"adapter {row['adapter_kind'] or 'unknown'} does not support "
            "task runtime overrides")

    import json

    allowed_models = set(json.loads(row["allowed_models_json"] or "[]"))
    allowed_efforts = set(json.loads(
        row["allowed_reasoning_efforts_json"] or "[]"))
    model = (requested or {}).get("model")
    effort = (requested or {}).get("reasoning_effort")
    if include_profile_defaults:
        model = model or row["model"]
        effort = effort or row["reasoning_effort"]
    if model is not None and model not in allowed_models:
        raise PermissionError(
            f"model is outside Agent Profile allowed_models: {model}")
    if effort is not None and effort not in allowed_efforts:
        raise PermissionError(
            "reasoning_effort is outside Agent Profile "
            f"allowed_reasoning_efforts: {effort}")
    if model is None and effort is None:
        return None
    return {
        **({"model": model} if model is not None else {}),
        **({"reasoning_effort": effort} if effort is not None else {}),
    }
