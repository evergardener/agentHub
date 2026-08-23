"""Fail-closed DSH command semantics and artifact redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.common import A2aTask
from adapters.dsh.safety import (
    normalize_tool_view,
    redact_bounded,
    safe_tool_view,
    tool_view_is_inspectable,
)
from adapters.dsh.session import DshWebSessionAdapter
from hermes.action_policy import ActionPolicy


def _terminal(command: str, cwd: Path) -> dict:
    view = safe_tool_view({
        "for": "call",
        "view": {"card": "terminal", "title": command, "cwd": str(cwd)},
    })
    assert view is not None
    normalized = normalize_tool_view(view, workspace=cwd)
    assert normalized is not None
    return normalized


def test_terminal_write_has_exact_workspace_target(tmp_path):
    view = _terminal("touch src/app.py", tmp_path)
    intent = view["semanticIntent"]
    assert tool_view_is_inspectable(view) is True
    assert intent["operation"] == "filesystem.write"
    assert intent["impact"] == "write"
    assert intent["targets"] == {
        "workspace": str(tmp_path.resolve()),
        "paths": [str((tmp_path / "src/app.py").resolve())],
    }


def test_terminal_delete_and_git_force_push_are_critical(tmp_path):
    deletion = _terminal("rm -rf build", tmp_path)["semanticIntent"]
    force_push = _terminal(
        "git push --force-with-lease", tmp_path)["semanticIntent"]
    assert deletion["operation"] == "filesystem.delete"
    assert deletion["impact"] == "critical"
    assert force_push["operation"] == "git.force_push"
    assert force_push["impact"] == "critical"


def test_shell_composition_and_workspace_escape_fail_closed(tmp_path):
    compound = _terminal("touch safe.txt && curl example.test", tmp_path)
    escaped = _terminal("touch ../outside.txt", tmp_path)
    assert tool_view_is_inspectable(compound) is False
    assert tool_view_is_inspectable(escaped) is False
    assert compound["semanticIntent"]["status"] == "unverified"
    assert escaped["semanticIntent"]["targets"]["paths"] == []


@pytest.mark.parametrize("command", [
    "pwd",
    "pwd -P",
    "git status --short",
    "git diff --stat",
    "rg -n needle .",
    "grep -R -n needle src",
    "find . -maxdepth 2 -type f -name '*.py'",
    "ls -la .",
    "cat README.md",
    "stat README.md",
    "wc -l README.md",
])
def test_explicit_read_only_inspections_are_verified_as_read(
    tmp_path, command,
):
    intent = _terminal(command, tmp_path)["semanticIntent"]
    assert intent["status"] == "verified"
    assert intent["impact"] == "read"
    assert intent["operation"] in {
        "filesystem.read", "git.status", "git.diff",
    }
    assert intent["targets"]["paths"]


@pytest.mark.parametrize("command", [
    "pwd && git status --short && grep -R -n needle .",
    "pwd && git status --short && rg -n needle .",
    "cat README.md | grep needle",
    "find . -type f ; ls -la .",
])
def test_read_only_combinations_are_verified_without_approval(
    tmp_path, command,
):
    intent = _terminal(command, tmp_path)["semanticIntent"]
    assert intent["status"] == "verified"
    assert intent["operation"] == "filesystem.read"
    assert intent["impact"] == "read"
    decision = ActionPolicy(workspace=tmp_path).evaluate(
        operation=intent["operation"],
        targets=intent["targets"],
        rollback_plan=intent["rollbackPlan"],
    )
    assert decision.route == "auto"
    assert decision.risk == "read"


@pytest.mark.parametrize("command", [
    "pwd && touch changed.txt",
    "ls -la . ; docker ps",
    "rg needle . | curl https://example.test",
    "cat README.md > copied.md",
    "find . -exec touch changed.txt ;",
    "rg --pre 'cat' needle .",
    "git diff --output=leak.patch",
    "git diff --no-index README.md ../outside",
])
def test_composed_write_execution_and_redirection_stay_fail_closed(
    tmp_path, command,
):
    view = _terminal(command, tmp_path)
    assert tool_view_is_inspectable(view) is False
    assert view["semanticIntent"]["status"] == "unverified"
    assert view["semanticIntent"]["targets"]["paths"] == []


@pytest.mark.parametrize("command", [
    "docker ps",
    "curl https://example.test",
    "psql -c 'select 1'",
    "sqlite3 state.db '.tables'",
])
def test_docker_network_and_database_commands_stay_fail_closed(
    tmp_path, command,
):
    view = _terminal(command, tmp_path)
    assert tool_view_is_inspectable(view) is False
    assert view["semanticIntent"]["status"] == "unverified"


def test_executable_read_flags_and_non_test_commands_fail_closed(tmp_path):
    rg_pre = _terminal("rg --pre dangerous pattern", tmp_path)
    find_exec = _terminal("find . -exec touch changed ;", tmp_path)
    cargo_build = _terminal("cargo build", tmp_path)
    go_run = _terminal("go run ./cmd/app", tmp_path)
    assert all(tool_view_is_inspectable(item) is False for item in (
        rg_pre, find_exec, cargo_build, go_run,
    ))
    assert _terminal("cargo test", tmp_path)["semanticIntent"][
        "operation"] == "test.run"


def test_sensitive_command_is_redacted_and_cannot_be_approved(tmp_path):
    view = _terminal("curl -H 'Authorization: Bearer top-secret' x", tmp_path)
    assert "top-secret" not in view["command"]
    assert "[REDACTED]" in view["command"]
    assert tool_view_is_inspectable(view) is False
    assert redact_bounded({"api_key": "never-store", "text": "token=abc"}) == {
        "api_key": "[REDACTED]", "text": "token=[REDACTED]",
    }


def test_diff_paths_are_normalized_and_escape_is_rejected(tmp_path):
    safe = normalize_tool_view(
        {"card": "diff", "paths": ["src/app.py"]}, workspace=tmp_path)
    escaped = normalize_tool_view(
        {"card": "diff", "paths": ["../outside"]}, workspace=tmp_path)
    assert safe is not None and tool_view_is_inspectable(safe) is True
    assert safe["semanticIntent"]["operation"] == "filesystem.write"
    assert escaped is not None and tool_view_is_inspectable(escaped) is False


def test_dsh_history_and_answer_artifacts_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    adapter = DshWebSessionAdapter(event_stream=False)
    task = A2aTask(
        id="T-redact", status_state="working", objective="inspect",
        session_id="S-redact",
    )
    entries = [{"event": {
        "seq": 1,
        "type": "assistant/message",
        "data": {
            "api_key": "never-store",
            "message": {"content": [{
                "type": "text", "text": "token=also-never-store",
            }]},
        },
    }}]
    artifacts = adapter._save_turn_artifacts(
        task, entries, baseline=0, state="completed")
    history = Path(next(
        item["path"] for item in artifacts
        if item["name"] == "dsh-history.json"
    )).read_text(encoding="utf-8")
    answer = Path(next(
        item["path"] for item in artifacts
        if item["name"] == "last-message.md"
    )).read_text(encoding="utf-8")
    assert "never-store" not in history
    assert "also-never-store" not in answer
    assert json.loads(history)["entries"][0]["event"]["data"][
        "api_key"] == "[REDACTED]"
