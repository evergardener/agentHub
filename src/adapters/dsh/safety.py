"""Fail-closed DSH tool inspection, target normalization, and redaction."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key)", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)"
    r"([^\s,;]+)")
_SHELL_CONTROL = re.compile(
    r"(?:[\r\n;&|<>`]|&&|\|\||\$\(|\$\{|\$[A-Za-z_])")
_UNRESOLVED_PATH = re.compile(r"[*?\[\]{}~]")


def redact_bounded(value: Any, *, limit: int = 8192) -> Any:
    """Bound recursive DSH data and redact common credential representations."""
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1[REDACTED]", value[:limit])
    if isinstance(value, list):
        return [redact_bounded(item, limit=limit) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: (
                "[REDACTED]" if _SENSITIVE_KEY.search(str(key))
                else redact_bounded(item, limit=limit)
            )
            for key, item in list(value.items())[:200]
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)[:limit]


def safe_tool_view(view: Any) -> dict[str, Any] | None:
    """Reduce DSH presentation data to bounded approval-safe fields."""
    if not isinstance(view, dict) or view.get("for") != "call":
        return None
    value = view.get("view")
    if not isinstance(value, dict):
        return None
    card = value.get("card")
    title = value.get("title")
    if card == "terminal" and isinstance(title, str) and title.strip():
        command = title[:8192]
        redacted = redact_bounded(command)
        cwd = value.get("cwd")
        cwd = cwd[:4096] if isinstance(cwd, str) else None
        safe_cwd = redact_bounded(cwd) if cwd is not None else None
        return {
            "card": card,
            "command": redacted,
            "cwd": safe_cwd,
            "redacted": redacted != command or safe_cwd != cwd,
        }
    if card == "diff":
        paths = [
            item.get("path") for item in value.get("diffs", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        safe_paths = [str(redact_bounded(path[:4096])) for path in paths[:100]]
        if safe_paths:
            return {
                "card": card,
                "title": redact_bounded(str(title or "file change")[:512]),
                "paths": safe_paths,
                "redacted": safe_paths != paths[:100],
            }
    if card == "generic" and isinstance(title, str) and title.strip():
        locations = [
            item.get("path") for item in value.get("locations", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        safe_paths = [
            str(redact_bounded(path[:4096])) for path in locations[:100]
        ]
        return {
            "card": card,
            "title": redact_bounded(title[:512]),
            "kind": value.get("kind"),
            "paths": safe_paths,
            "redacted": safe_paths != locations[:100],
        }
    return None


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_paths(
    values: list[str], *, cwd: Path, workspace: Path,
) -> list[str] | None:
    resolved: list[str] = []
    for value in values:
        if (not value or value.startswith("-")
                or _UNRESOLVED_PATH.search(value)
                or "$" in value or "\x00" in value):
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve()
        if not _inside(candidate, workspace):
            return None
        text = str(candidate)
        if text not in resolved:
            resolved.append(text)
    return resolved


def _verified(
    operation: str, impact: str, paths: list[str], workspace: Path,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": "verified",
        "operation": operation,
        "impact": impact,
        "targets": {"workspace": str(workspace), "paths": paths},
        "rollbackPlan": None,
        "reason": reason,
    }


def _unverified(reason: str) -> dict[str, Any]:
    return {
        "status": "unverified",
        "operation": "agent.tool.unknown",
        "impact": "unknown",
        "targets": {"paths": []},
        "rollbackPlan": None,
        "reason": reason,
    }


def _terminal_intent(
    view: dict[str, Any], workspace: Path,
) -> dict[str, Any]:
    command = view.get("command")
    if not isinstance(command, str) or not command.strip():
        return _unverified("terminal command is missing")
    if view.get("redacted") is True:
        return _unverified("terminal command contained redacted sensitive data")
    if _SHELL_CONTROL.search(command):
        return _unverified(
            "shell composition, expansion, or redirection is unsupported")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return _unverified("terminal command could not be parsed")
    if not tokens:
        return _unverified("terminal command is empty")

    raw_cwd = view.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return _unverified("terminal cwd is missing")
    cwd = Path(raw_cwd).expanduser().resolve()
    if not _inside(cwd, workspace):
        return _unverified("terminal cwd is outside the task workspace")

    executable = Path(tokens[0]).name
    args = tokens[1:]
    if executable in {"sudo", "su", "env", "sh", "bash", "zsh", "fish"}:
        return _unverified("shell or privilege wrapper is unsupported")

    if executable == "git":
        if not args or args[0].startswith("-"):
            return _unverified("git subcommand is missing or changes repository scope")
        subcommand = args[0]
        operation = {
            "status": "git.status", "diff": "git.diff",
            "log": "filesystem.read", "show": "filesystem.read",
            "commit": "git.commit", "push": "git.push",
        }.get(subcommand)
        if operation is None:
            return _unverified(f"unsupported git subcommand: {subcommand}")
        if subcommand == "push" and any(
                item in {"-f", "--force", "--force-with-lease"}
                for item in args[1:]):
            operation = "git.force_push"
        impact = "read" if operation in {
            "git.status", "git.diff", "filesystem.read"} else "critical"
        return _verified(operation, impact, [str(cwd)], workspace,
                         f"recognized git {subcommand}")

    joined = " ".join(tokens)
    if (executable == "pytest"
            or (executable in {"cargo", "go"} and args[:1] == ["test"])
            or (executable in {"python", "python3"}
                and args[:2] == ["-m", "pytest"])
            or (executable in {"npm", "pnpm", "yarn"}
                and any(item == "test" for item in args[:2]))):
        return _verified("test.run", "read", [str(cwd)], workspace,
                         f"recognized test command: {joined[:128]}")

    if executable == "pwd" and not args:
        return _verified("filesystem.read", "read", [str(cwd)], workspace,
                         f"recognized read-only command: {executable}")

    if executable in {"rg", "grep"}:
        if not args or any(item.startswith("-") for item in args):
            return _unverified(f"{executable} options are not normalized")
        paths = _resolve_paths(args[1:] or ["."], cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified(f"{executable} target could not be normalized")
        return _verified("filesystem.read", "read", paths, workspace,
                         f"recognized search command: {executable}")

    if executable == "find":
        if len(args) != 1 or args[0].startswith("-"):
            return _unverified("find predicates are not normalized")
        paths = _resolve_paths(args, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified("find target could not be normalized")
        return _verified("filesystem.read", "read", paths, workspace,
                         "recognized bounded find target")

    if executable == "ls":
        operands = [item for item in args if not item.startswith("-")] or ["."]
        paths = _resolve_paths(operands, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified("ls target could not be normalized")
        return _verified("filesystem.read", "read", paths, workspace,
                         "recognized directory listing")

    if executable in {"cat", "stat", "wc"}:
        if not args or any(item.startswith("-") for item in args):
            return _unverified(f"{executable} options are not normalized")
        paths = _resolve_paths(args, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified(f"{executable} target could not be normalized")
        return _verified("filesystem.read", "read", paths, workspace,
                         f"recognized file read: {executable}")

    write_specs = {
        "touch": ("filesystem.write", set()),
        "mkdir": ("filesystem.create", {"-p"}),
        "rm": ("filesystem.delete", {"-f", "-r", "-rf", "-fr"}),
        "rmdir": ("filesystem.delete", set()),
        "unlink": ("filesystem.delete", set()),
    }
    if executable in write_specs:
        operation, allowed_options = write_specs[executable]
        options = {item for item in args if item.startswith("-")}
        if options - allowed_options:
            return _unverified(f"unsupported {executable} options")
        operands = [item for item in args if not item.startswith("-")]
        if not operands:
            return _unverified(f"{executable} target is missing")
        paths = _resolve_paths(operands, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified(f"{executable} target could not be normalized")
        impact = "critical" if operation == "filesystem.delete" else "write"
        return _verified(operation, impact, paths, workspace,
                         f"recognized filesystem command: {executable}")

    return _unverified(f"unsupported terminal command: {executable}")


def normalize_tool_view(
    view: dict[str, Any] | None, *, workspace: Path,
) -> dict[str, Any] | None:
    """Attach a fail-closed semantic intent scoped to one task workspace."""
    if view is None:
        return None
    prepared = dict(view)
    root = workspace.expanduser().resolve()
    card = prepared.get("card")
    if prepared.get("redacted") is True:
        intent = _unverified("tool target or command contained sensitive data")
    elif card == "terminal":
        intent = _terminal_intent(prepared, root)
    elif card == "diff":
        paths = _resolve_paths(
            prepared.get("paths") or [], cwd=root, workspace=root)
        intent = (
            _verified("filesystem.write", "write", paths, root,
                      "recognized DSH diff targets")
            if paths else _unverified("diff targets could not be normalized")
        )
    elif card == "generic":
        kind = str(prepared.get("kind") or "").lower()
        paths = _resolve_paths(
            prepared.get("paths") or [], cwd=root, workspace=root)
        operation = {
            "read": "filesystem.read", "search": "filesystem.read",
            "edit": "filesystem.write", "write": "filesystem.write",
            "delete": "filesystem.delete", "test": "test.run",
        }.get(kind)
        if operation and paths:
            impact = (
                "read" if operation in {"filesystem.read", "test.run"}
                else "critical" if operation == "filesystem.delete"
                else "write"
            )
            intent = _verified(operation, impact, paths, root,
                               f"recognized generic tool kind: {kind}")
        else:
            intent = _unverified("generic tool kind or targets are unsupported")
    else:
        intent = _unverified("tool presentation card is unsupported")
    prepared["semanticIntent"] = intent
    return prepared


def tool_view_is_inspectable(view: dict[str, Any] | None) -> bool:
    return bool(
        view
        and isinstance(view.get("semanticIntent"), dict)
        and view["semanticIntent"].get("status") == "verified"
    )
