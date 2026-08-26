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
_UNRESOLVED_PATH = re.compile(r"[*?\[\]{}~]")
_READ_ONLY_COMPOUND_OPERATIONS = frozenset({
    "filesystem.read", "git.status", "git.diff",
})
_COMPOUND_OPERATORS = frozenset({"&&", ";", "|"})

# Docker is a daemon-facing command, so its basename must not by itself make
# the request safe.  Keep this recognizer deliberately smaller than Docker's
# CLI: only metadata/log reads with bounded, explicitly parsed arguments may
# become ``command.read``.  In particular, no global daemon/context flags,
# shell wrappers, compose commands, exec, streaming logs, or lifecycle/image
# mutation commands are accepted here.
_DOCKER_READ_FLAGS = {
    "ps": frozenset({
        "-a", "--all", "-q", "--quiet", "--no-trunc", "--size",
        "-s", "--latest", "-l", "--noheading",
    }),
    "inspect": frozenset({"-s", "--size"}),
    "logs": frozenset({"--details", "-t", "--timestamps"}),
    "stats": frozenset({"--no-stream", "-a", "--all"}),
    "images": frozenset({
        "-a", "--all", "--no-trunc", "--digests",
    }),
    "version": frozenset(),
}
_DOCKER_READ_VALUE_FLAGS = {
    "ps": frozenset({"-f", "--filter", "--format"}),
    "inspect": frozenset({"-f", "--format", "--type", "--platform"}),
    "logs": frozenset({"--since", "--until", "--tail"}),
    "stats": frozenset({"--format"}),
    "images": frozenset({"-f", "--filter", "--format"}),
    "version": frozenset({"--format"}),
}
_DOCKER_READ_SUBCOMMANDS = frozenset(_DOCKER_READ_FLAGS)

_SEARCH_FLAG_ONLY = {
    "rg": frozenset({
        "-n", "--line-number", "-i", "--ignore-case", "-s",
        "--case-sensitive", "-S", "--smart-case", "-F",
        "--fixed-strings", "-w", "--word-regexp", "-x",
        "--line-regexp", "-l", "--files-with-matches",
        "--files-without-match", "-c", "--count", "--count-matches",
        "-o", "--only-matching", "-q", "--quiet", "--hidden",
        "--no-hidden", "--no-ignore", "--no-ignore-vcs", "-L",
        "--follow", "--files", "-0", "--null", "--null-data",
        "--json", "--stats", "--trim", "-U", "--multiline",
        "--multiline-dotall", "-a", "--text", "--crlf",
        "--no-messages",
    }),
    "grep": frozenset({
        "-n", "--line-number", "-i", "--ignore-case", "-v",
        "--invert-match", "-w", "--word-regexp", "-x",
        "--line-regexp", "-F", "--fixed-strings", "-E",
        "--extended-regexp", "-G", "--basic-regexp", "-P",
        "--perl-regexp", "-R", "--dereference-recursive", "-r",
        "--recursive", "-l", "--files-with-matches", "-L",
        "--files-without-match", "-c", "--count", "-o",
        "--only-matching", "-q", "--quiet", "-s", "--no-messages",
        "-H", "--with-filename", "-h", "--no-filename", "-a",
        "--text", "-I", "--binary-files=without-match",
    }),
}
_SEARCH_VALUE_FLAGS = {
    "rg": frozenset({
        "-g", "--glob", "-t", "--type", "-T", "--type-not", "-A",
        "--after-context", "-B", "--before-context", "-C", "--context",
        "-m", "--max-count", "--max-depth", "--max-filesize", "-e",
        "--regexp", "--sort", "--sortr", "--threads", "--encoding",
        "--engine", "--color", "--colors",
    }),
    "grep": frozenset({
        "-A", "--after-context", "-B", "--before-context", "-C",
        "--context", "-m", "--max-count", "-e", "--regexp",
        "--include", "--exclude", "--exclude-dir", "--binary-files",
        "--color",
    }),
}
_SEARCH_PATH_VALUE_FLAGS = {
    "rg": frozenset({"-f", "--file", "--ignore-file"}),
    "grep": frozenset({"-f", "--file", "--exclude-from"}),
}
_SEARCH_SHORT_BUNDLE = {
    "rg": re.compile(r"-[nHiSsFwxloqUa0cL]+"),
    "grep": re.compile(r"-[nHhIiVvwxFEPGRrslLcoqsa]+"),
}

_FIND_VALUE_PREDICATES = frozenset({
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex",
    "-type", "-size", "-mtime", "-mmin", "-atime", "-amin",
    "-ctime", "-cmin", "-user", "-group", "-uid", "-gid", "-perm",
    "-links", "-maxdepth", "-mindepth", "-printf",
})
_FIND_FLAG_PREDICATES = frozenset({
    "-print", "-print0", "-empty", "-readable", "-writable",
    "-executable", "-true", "-false", "-ls", "-mount", "-xdev",
    "-depth", "!", "-not", "-a", "-and", "-o", "-or", "(", ")",
})


def redact_bounded(value: Any, *, limit: int = 8192,
                   max_items: int = 200) -> Any:
    """Bound recursive DSH data and redact common credential representations."""
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1[REDACTED]", value[:limit])
    if isinstance(value, list):
        return [redact_bounded(item, limit=limit, max_items=max_items)
                for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key)[:128]: (
                "[REDACTED]" if _SENSITIVE_KEY.search(str(key))
                else redact_bounded(
                    item, limit=limit, max_items=max_items)
            )
            for key, item in list(value.items())[:max_items]
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


def _has_unsafe_shell_syntax(command: str) -> bool:
    """Reject expansion/redirection while allowing quoted search patterns."""
    quote: str | None = None
    escaped = False
    for char in command:
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
        if char in "\r\n":
            return True
        if quote != "'" and char in {"$", "`"}:
            return True
        if quote is None and char in {"<", ">"}:
            return True
    return False


def _search_paths(
    executable: str, args: list[str], *, cwd: Path, workspace: Path,
) -> list[str] | None:
    positionals: list[str] = []
    option_paths: list[str] = []
    explicit_pattern = False
    files_mode = False
    index = 0
    options_done = False
    while index < len(args):
        item = args[index]
        if options_done:
            positionals.append(item)
            index += 1
            continue
        if item == "--":
            options_done = True
            index += 1
            continue
        name, equals, inline_value = item.partition("=")
        if item in _SEARCH_FLAG_ONLY[executable]:
            files_mode = files_mode or (executable == "rg" and item == "--files")
            index += 1
            continue
        if _SEARCH_SHORT_BUNDLE[executable].fullmatch(item):
            index += 1
            continue
        value_kind = None
        if name in _SEARCH_VALUE_FLAGS[executable]:
            value_kind = "value"
        elif name in _SEARCH_PATH_VALUE_FLAGS[executable]:
            value_kind = "path"
        if value_kind:
            if equals:
                value = inline_value
            elif index + 1 < len(args):
                index += 1
                value = args[index]
            else:
                return None
            if not value:
                return None
            if name in {"-e", "--regexp"}:
                explicit_pattern = True
            if value_kind == "path":
                option_paths.append(value)
            index += 1
            continue
        if item.startswith("-"):
            return None
        positionals.append(item)
        index += 1

    if files_mode:
        operands = positionals or ["."]
    elif explicit_pattern:
        operands = positionals or ["."]
    else:
        if not positionals:
            return None
        operands = positionals[1:] or ["."]
    return _resolve_paths(
        option_paths + operands, cwd=cwd, workspace=workspace)


def _find_paths(
    args: list[str], *, cwd: Path, workspace: Path,
) -> list[str] | None:
    roots: list[str] = []
    index = 0
    while (index < len(args)
           and not args[index].startswith("-")
           and args[index] not in {"!", "(", ")"}):
        roots.append(args[index])
        index += 1
    roots = roots or ["."]
    paths = _resolve_paths(roots, cwd=cwd, workspace=workspace)
    if paths is None:
        return None
    while index < len(args):
        item = args[index]
        if item in _FIND_FLAG_PREDICATES:
            index += 1
            continue
        if item not in _FIND_VALUE_PREDICATES or index + 1 >= len(args):
            return None
        value = args[index + 1]
        if not value or "\x00" in value:
            return None
        if item in {"-maxdepth", "-mindepth"} and not value.isdigit():
            return None
        index += 2
    return paths


def _docker_option_value_safe(value: str) -> bool:
    """Bound values for Docker's read-only formatting/filter options."""
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= 4096
        and not value.startswith("-")
        and not any(char in value for char in ("\x00", "\r", "\n"))
    )


def _docker_read_args(
    subcommand: str, args: list[str],
) -> tuple[list[str], set[str]] | None:
    """Parse one restricted Docker read command's options and operands.

    This intentionally does not accept Docker global options.  Rejecting them
    prevents a seemingly read-only command from selecting another daemon,
    config, or TLS credential set.  Option values are consumed as argv items,
    never interpreted as shell syntax.
    """
    flags = _DOCKER_READ_FLAGS[subcommand]
    value_flags = _DOCKER_READ_VALUE_FLAGS[subcommand]
    operands: list[str] = []
    seen: set[str] = set()
    index = 0
    options_done = False
    while index < len(args):
        item = args[index]
        if options_done or not item.startswith("-"):
            if (not item or len(item) > 256
                    or "\x00" in item):
                return None
            operands.append(item)
            index += 1
            continue
        if item == "--":
            options_done = True
            index += 1
            continue

        name, equals, inline_value = item.partition("=")
        if name in value_flags:
            if name in seen:
                return None
            if equals:
                value = inline_value
            elif index + 1 < len(args):
                index += 1
                value = args[index]
            else:
                return None
            if not _docker_option_value_safe(value):
                return None
            seen.add(name)
            index += 1
            continue
        if item not in flags or item in seen:
            # This also rejects all unrecognized Docker global options, such
            # as --context/--host/--config/--tls*, before a subcommand.
            return None
        seen.add(item)
        index += 1
    return operands, seen


def _docker_read_intent(
    tokens: list[str], *, cwd: Path, workspace: Path,
) -> dict[str, Any]:
    """Recognize only bounded, one-shot Docker daemon reads."""
    if len(tokens) < 2 or Path(tokens[0]).name != "docker":
        return _unverified("unsupported terminal command")
    subcommand = tokens[1]
    if subcommand not in _DOCKER_READ_SUBCOMMANDS:
        return _unverified(
            f"unsupported or modifying Docker subcommand: {subcommand}")
    parsed = _docker_read_args(subcommand, tokens[2:])
    if parsed is None:
        return _unverified(
            f"Docker {subcommand} options or targets are not read-only")
    operands, seen = parsed

    # Keep the accepted argv shape explicit per command.  This avoids turning
    # the operation into an arbitrary Docker passthrough while still covering
    # the common discovery and log-reading requests.
    if subcommand == "ps" and operands:
        return _unverified("docker ps does not accept positional targets")
    if subcommand == "inspect" and not operands:
        return _unverified("docker inspect target is missing")
    if subcommand == "logs" and len(operands) != 1:
        return _unverified("docker logs requires exactly one container")
    if subcommand == "stats" and "--no-stream" not in seen:
        return _unverified("docker stats must use --no-stream")
    if subcommand == "stats" and len(operands) > 100:
        return _unverified("docker stats has too many targets")
    if subcommand == "images" and len(operands) > 1:
        return _unverified("docker images accepts at most one repository")
    if subcommand == "version" and operands:
        return _unverified("docker version does not accept positional targets")

    intent = _verified(
        "command.read", "read", [str(cwd)], workspace,
        f"recognized bounded Docker {subcommand} inspection",
    )
    intent["targets"].update({
        "cwd": str(cwd),
        "command": "docker",
        "args": tokens[1:],
    })
    return intent


def _simple_terminal_intent(
    tokens: list[str], *, cwd: Path, workspace: Path,
) -> dict[str, Any]:
    executable = Path(tokens[0]).name
    args = tokens[1:]
    if executable in {"sudo", "su", "env", "sh", "bash", "zsh", "fish"}:
        return _unverified("shell or privilege wrapper is unsupported")

    if executable == "docker":
        return _docker_read_intent(tokens, cwd=cwd, workspace=workspace)

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
        unsafe_read_flags = {
            "--no-index", "--ext-diff", "--textconv", "--output", "-o",
        }
        if (subcommand in {"status", "diff", "log", "show"}
                and any(
                    item in unsafe_read_flags
                    or any(
                        item.startswith(f"{flag}=")
                        for flag in unsafe_read_flags)
                    or item in {"-C", "--git-dir", "--work-tree",
                                "--config-env", "--exec-path"}
                    or item.startswith(("--git-dir=", "--work-tree=",
                                         "--config-env=", "--exec-path="))
                    for item in args[1:])):
            return _unverified("git scope, execution, or output option is unsafe")
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

    if executable == "pwd" and all(item in {"-L", "-P"} for item in args):
        return _verified("filesystem.read", "read", [str(cwd)], workspace,
                         f"recognized read-only command: {executable}")

    if executable in {"rg", "grep"}:
        paths = _search_paths(
            executable, args, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified(
                f"{executable} options or targets could not be normalized")
        return _verified("filesystem.read", "read", paths, workspace,
                         f"recognized search command: {executable}")

    if executable == "find":
        paths = _find_paths(args, cwd=cwd, workspace=workspace)
        if paths is None:
            return _unverified("find predicates or targets are not read-only")
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
        safe_options = {
            "cat": {
                "-A", "-b", "-e", "-E", "-n", "-s", "-t", "-T", "-u",
                "-v",
            },
            "stat": {
                "-L", "--dereference", "-f", "--file-system", "-t",
                "--terse",
            },
            "wc": {"-c", "--bytes", "-m", "--chars", "-l", "--lines",
                   "-L", "--max-line-length", "-w", "--words"},
        }[executable]
        operands = [item for item in args if not item.startswith("-")]
        options = [item for item in args if item.startswith("-")]
        if not operands or any(item not in safe_options for item in options):
            return _unverified(f"{executable} options are not normalized")
        paths = _resolve_paths(operands, cwd=cwd, workspace=workspace)
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


def _terminal_intent(
    view: dict[str, Any], workspace: Path,
) -> dict[str, Any]:
    command = view.get("command")
    if not isinstance(command, str) or not command.strip():
        return _unverified("terminal command is missing")
    if view.get("redacted") is True:
        return _unverified("terminal command contained redacted sensitive data")
    if _has_unsafe_shell_syntax(command):
        return _unverified("shell expansion or redirection is unsupported")

    try:
        lexer = shlex.shlex(
            command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
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

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _COMPOUND_OPERATORS:
            if not current:
                return _unverified("shell composition contains an empty command")
            segments.append(current)
            current = []
        elif token and all(char in ";&|<>" for char in token):
            return _unverified(f"unsupported shell operator: {token}")
        else:
            current.append(token)
    if not current:
        return _unverified("shell composition contains an empty command")
    segments.append(current)

    intents = [
        _simple_terminal_intent(segment, cwd=cwd, workspace=workspace)
        for segment in segments
    ]
    if len(intents) == 1:
        return intents[0]
    if any(intent.get("status") != "verified" for intent in intents):
        return _unverified("compound command contains an unverified operation")
    if any(
        intent.get("impact") != "read"
        or intent.get("operation") not in _READ_ONLY_COMPOUND_OPERATIONS
        for intent in intents
    ):
        return _unverified("compound command contains a non-read-only operation")
    paths: list[str] = []
    for intent in intents:
        for path in intent["targets"]["paths"]:
            if path not in paths:
                paths.append(path)
    return _verified(
        "filesystem.read", "read", paths, workspace,
        f"recognized read-only command sequence ({len(intents)} commands)")


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
