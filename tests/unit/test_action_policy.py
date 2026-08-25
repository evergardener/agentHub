"""Structured ActionIntent routing and authority tests."""

from __future__ import annotations

import pytest

from hermes.action_policy import ActionPolicy


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_explicit_read_is_auto(workspace):
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation="filesystem.read", targets=["src/app.py"],
        rollback_plan=None)
    assert decision.route == "auto"
    assert decision.risk == "read"


def test_read_outside_workspace_routes_to_user(workspace):
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation="filesystem.read",
        targets=[str(workspace.parent / "secret.txt")],
        rollback_plan=None)
    assert decision.route == "user"
    assert "超出工作区" in decision.reason


def test_workspace_write_with_rollback_routes_to_hermes(workspace):
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation="filesystem.write", targets=["src/app.py"],
        rollback_plan="git restore src/app.py")
    assert decision.route == "hermes"
    assert decision.risk == "write"


@pytest.mark.parametrize("operation", [
    "filesystem.delete", "git.push", "deployment.apply",
    "database.write", "secret.access", "command.execute",
])
def test_critical_operations_route_to_user(workspace, operation):
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation=operation, targets=["src/app.py"], rollback_plan="undo")
    assert decision.route == "user"
    assert decision.risk == "critical"


def test_unknown_operation_fails_closed(workspace):
    decision = ActionPolicy(workspace=workspace).evaluate(
        operation="shell.do_something", targets=["src/app.py"],
        rollback_plan="undo")
    assert decision.route == "user"
    assert decision.risk == "unknown"


def test_outside_workspace_and_missing_rollback_route_to_user(workspace):
    policy = ActionPolicy(workspace=workspace)
    outside = policy.evaluate(
        operation="filesystem.write", targets=[str(workspace.parent / "x")],
        rollback_plan="undo")
    no_rollback = policy.evaluate(
        operation="filesystem.write", targets=["src/app.py"],
        rollback_plan=None)
    assert outside.route == "user" and "超出工作区" in outside.reason
    assert no_rollback.route == "user" and "回滚" in no_rollback.reason


def test_duplicate_policy_operation_rejected(workspace, tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text(
        "auto: [filesystem.read]\n"
        "hermes_approve: [filesystem.read]\n"
        "require_user: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="multiple policy groups"):
        ActionPolicy(workspace=workspace, permissions_path=config)
