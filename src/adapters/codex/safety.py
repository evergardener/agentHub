"""Fail-closed Codex approval normalization scoped to one task workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters.dsh.safety import normalize_tool_view as normalize_shell_view


def _unverified(reason: str) -> dict[str, Any]:
    return {
        "status": "unverified",
        "operation": "agent.tool.unknown",
        "impact": "unknown",
        "targets": {"paths": []},
        "rollbackPlan": None,
        "reason": reason,
    }


def _resolve_paths(values: Any, workspace: Path) -> list[str] | None:
    if not isinstance(values, list) or not values:
        return None
    resolved: list[str] = []
    for value in values:
        if (not isinstance(value, str) or not value
                or any(char in value for char in ("\x00", "$", "*", "?"))):
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve(strict=False)
        if path != workspace and workspace not in path.parents:
            return None
        text = str(path)
        if text not in resolved:
            resolved.append(text)
    return resolved


def _file_change_intent(view: dict[str, Any], workspace: Path) -> dict[str, Any]:
    changes = view.get("changes")
    if not isinstance(changes, list) or not changes:
        return _unverified("Codex file change details are missing")
    change_paths: list[str] = []
    kinds: set[str] = set()
    reversible = True
    for change in changes:
        if not isinstance(change, dict):
            return _unverified("Codex file change details are malformed")
        path = change.get("path")
        kind_value = change.get("kind")
        kind = (
            kind_value.get("type")
            if isinstance(kind_value, dict) else change.get("type")
        )
        if not isinstance(path, str) or kind not in {"add", "update", "delete"}:
            return _unverified("Codex file change path or type is unsupported")
        move_path = (
            kind_value.get("move_path")
            if isinstance(kind_value, dict) else
            change.get("move_path") or change.get("movePath")
        )
        if move_path:
            return _unverified("Codex file moves require user review")
        kinds.add(kind)
        change_paths.append(path)
        if kind in {"add", "update"}:
            reversible = reversible and isinstance(
                change.get("diff") or change.get("unified_diff")
                or change.get("unifiedDiff"), str)
        else:
            reversible = False
    paths = _resolve_paths(change_paths, workspace)
    declared = _resolve_paths(view.get("paths"), workspace)
    if paths is None or declared is None or set(paths) != set(declared):
        return _unverified(
            "Codex file change targets are outside or inconsistent with workspace")
    if "delete" in kinds:
        operation = "filesystem.delete"
        impact = "critical"
        rollback = None
    elif kinds == {"add"}:
        operation = "filesystem.create"
        impact = "write"
        rollback = (
            "Remove only the files created by this exact native Codex request"
            if reversible else None
        )
    else:
        operation = "filesystem.write"
        impact = "write"
        rollback = (
            "Reverse the exact native Codex patch before any later workspace write"
            if reversible else None
        )
    return {
        "status": "verified",
        "operation": operation,
        "impact": impact,
        "targets": {"workspace": str(workspace), "paths": paths},
        "rollbackPlan": rollback,
        "reason": "verified native Codex file-change request",
    }


def normalize_tool_view(
    view: dict[str, Any] | None, *, workspace: Path,
) -> dict[str, Any] | None:
    """Attach a structured intent without trusting adapter-authored semantics."""
    if view is None:
        return None
    prepared = dict(view)
    root = workspace.expanduser().resolve(strict=False)
    kind = prepared.get("kind")
    if kind == "shell":
        normalized = normalize_shell_view({
            "card": "terminal",
            "command": prepared.get("command"),
            "cwd": prepared.get("cwd"),
            "redacted": prepared.get("redacted", False),
        }, workspace=root)
        intent = (
            normalized.get("semanticIntent") if normalized else
            _unverified("Codex shell request could not be normalized")
        )
    elif kind == "edit":
        intent = _file_change_intent(prepared, root)
    elif kind == "permissions":
        paths = _resolve_paths(prepared.get("paths"), root)
        intent = ({
            "status": "verified",
            "operation": "grant.create",
            "impact": "critical",
            "targets": {"workspace": str(root), "paths": paths},
            "rollbackPlan": None,
            "reason": "Codex requested additional runtime permissions",
        } if paths else _unverified(
            "Codex permission request targets could not be normalized"))
    else:
        intent = _unverified("Codex approval kind is unsupported")
    prepared["semanticIntent"] = intent
    return prepared


def tool_view_is_inspectable(view: dict[str, Any] | None) -> bool:
    return bool(
        view
        and isinstance(view.get("semanticIntent"), dict)
        and view["semanticIntent"].get("status") == "verified"
    )
