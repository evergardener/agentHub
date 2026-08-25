"""Fail-closed Codex approval normalization scoped to one task workspace."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from adapters.dsh.safety import normalize_tool_view as normalize_shell_view

_CODEX_LOGIN_SHELLS = frozenset({"/bin/sh", "/bin/bash", "/bin/zsh"})
_SHELL_OR_PRIVILEGE_WRAPPERS = frozenset({
    ".", "bash", "builtin", "cd", "chroot", "command", "csh", "dash",
    "declare", "direnv", "doas", "dotenv", "env", "envdir", "eval",
    "exec", "export", "fish", "ksh", "local", "nsenter", "pkexec",
    "powershell", "pwsh", "readonly", "runuser", "set", "setpriv", "sh",
    "source", "su", "sudo", "tcsh", "trap", "typeset", "ulimit", "umask",
    "unset", "zsh",
})
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


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


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _unsafe_login_shell_body(body: str) -> str | None:
    """Return why a shell body cannot be reduced to one exact argv."""
    quote: str | None = None
    escaped = False
    for char in body:
        if char in {"\x00", "\r", "\n"}:
            return "shell body contains a control character or newline"
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if quote != "'" and char in {"$", "`"}:
            return "shell expansion or command substitution is unsupported"
        if quote is None and char in ";&|<>(){}#!*?[]~":
            return "shell operators, comments, or expansion are unsupported"
    if escaped or quote is not None:
        return "shell body contains an incomplete escape or quote"
    return None


def _restricted_login_shell_intent(
    view: dict[str, Any], workspace: Path,
) -> dict[str, Any] | None:
    """Unwrap exactly one Codex ``/bin/* -lc`` request, or return None.

    ``None`` means this is not one of the explicitly supported outer shells;
    the existing fail-closed terminal normalizer remains authoritative.
    """
    command = view.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        outer = shlex.split(command, comments=False, posix=True)
    except ValueError:
        if any(command.lstrip().startswith(shell) for shell in
               _CODEX_LOGIN_SHELLS):
            return _unverified("Codex login-shell wrapper could not be parsed")
        return None
    if not outer or outer[0] not in _CODEX_LOGIN_SHELLS:
        return None
    if len(outer) != 3 or outer[1] != "-lc":
        return _unverified(
            "Codex login-shell wrapper must be exactly /bin/{sh,bash,zsh} "
            "-lc with one body")

    body = outer[2]
    unsafe_reason = _unsafe_login_shell_body(body)
    if unsafe_reason:
        return _unverified(unsafe_reason)
    try:
        tokens = shlex.split(body, comments=False, posix=True)
    except ValueError:
        return _unverified("Codex login-shell body could not be parsed")
    if not tokens or not tokens[0]:
        return _unverified("Codex login-shell body is empty")
    executable = Path(tokens[0]).name
    if (executable in _SHELL_OR_PRIVILEGE_WRAPPERS
            or _ENV_ASSIGNMENT.fullmatch(tokens[0])):
        return _unverified("shell or privilege wrapper is unsupported")

    raw_cwd = view.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return _unverified("terminal cwd is missing")
    cwd = Path(raw_cwd).expanduser().resolve(strict=False)
    if not _inside(cwd, workspace):
        return _unverified("terminal cwd is outside the task workspace")

    inner = normalize_shell_view({
        "card": "terminal", "command": body, "cwd": str(cwd),
        "redacted": False,
    }, workspace=workspace)
    inner_intent = inner.get("semanticIntent") if inner else None
    if (isinstance(inner_intent, dict)
            and inner_intent.get("status") == "verified"):
        intent = dict(inner_intent)
        targets = dict(intent.get("targets") or {})
    else:
        intent = {
            "status": "verified",
            "operation": "command.execute",
            "impact": "critical",
            "rollbackPlan": None,
            "reason": "verified single native Codex command; user approval required",
        }
        targets = {
            "workspace": str(workspace), "paths": [str(cwd)],
        }
    targets.update({
        "workspace": str(workspace),
        "paths": targets.get("paths") or [str(cwd)],
        "cwd": str(cwd),
        "command": tokens[0],
        "args": tokens[1:],
    })
    intent["targets"] = targets
    return intent


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
        if (prepared.get("redacted") is not True
                and intent.get("status") != "verified"):
            restricted = _restricted_login_shell_intent(prepared, root)
            if restricted is not None:
                intent = restricted
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
