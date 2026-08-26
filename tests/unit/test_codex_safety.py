import shlex

import pytest

from adapters.codex.safety import normalize_tool_view, tool_view_is_inspectable


def test_file_change_patch_is_workspace_scoped_and_reversible(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "src" / "app.py"
    view = normalize_tool_view({
        "kind": "edit",
        "paths": [str(target)],
        "changes": [{
            "path": str(target), "kind": {"type": "update"},
            "diff": "@@ -1 +1 @@\n-old\n+new",
        }],
    }, workspace=workspace)

    intent = view["semanticIntent"]
    assert intent["status"] == "verified"
    assert intent["operation"] == "filesystem.write"
    assert intent["targets"] == {
        "workspace": str(workspace.resolve()),
        "paths": [str(target.resolve())],
    }
    assert intent["rollbackPlan"]


def test_file_change_outside_workspace_fails_closed(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    view = normalize_tool_view({
        "kind": "edit",
        "paths": [str(tmp_path / "outside.py")],
        "changes": [{"path": str(tmp_path / "outside.py"),
                     "kind": {"type": "update"}, "diff": "@@"}],
    }, workspace=workspace)

    assert view["semanticIntent"]["status"] == "unverified"


def test_delete_and_permission_grant_never_route_as_safe_write(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    deleted = normalize_tool_view({
        "kind": "edit", "paths": [str(workspace / "old.py")],
        "changes": [{"path": str(workspace / "old.py"),
                     "kind": {"type": "delete"}, "diff": "@@"}],
    }, workspace=workspace)
    grant = normalize_tool_view({
        "kind": "permissions", "paths": [str(workspace)],
        "permissions": {"fileSystem": {"write": [str(workspace)]}},
    }, workspace=workspace)

    assert deleted["semanticIntent"]["operation"] == "filesystem.delete"
    assert deleted["semanticIntent"]["rollbackPlan"] is None
    assert grant["semanticIntent"]["operation"] == "grant.create"


def test_shell_uses_fail_closed_command_parser(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    read = normalize_tool_view({
        "kind": "shell", "command": "git status --short",
        "cwd": str(workspace),
    }, workspace=workspace)
    unknown = normalize_tool_view({
        "kind": "shell", "command": "curl https://example.test",
        "cwd": str(workspace),
    }, workspace=workspace)

    assert read["semanticIntent"]["operation"] == "git.status"
    assert read["semanticIntent"]["status"] == "verified"
    assert unknown["semanticIntent"]["status"] == "unverified"


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh"])
def test_codex_single_layer_login_shell_command_is_structured(
        tmp_path, shell):
    workspace = tmp_path / "project"
    cwd = workspace / "service"
    cwd.mkdir(parents=True)
    body = "docker build --tag agenthub:test ."

    view = normalize_tool_view({
        "kind": "shell",
        "command": f"{shell} -lc {shlex.quote(body)}",
        "cwd": str(cwd),
    }, workspace=workspace)

    intent = view["semanticIntent"]
    assert tool_view_is_inspectable(view) is True
    assert intent["status"] == "verified"
    assert intent["operation"] == "command.execute"
    assert intent["impact"] == "critical"
    assert intent["targets"] == {
        "workspace": str(workspace.resolve()),
        "paths": [str(cwd.resolve())],
        "cwd": str(cwd.resolve()),
        "command": "docker",
        "args": ["build", "--tag", "agenthub:test", "."],
    }
    assert intent["rollbackPlan"] is None


def test_codex_login_shell_keeps_known_inner_command_policy(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    body = "python -m pytest tests/unit/test_app.py"

    view = normalize_tool_view({
        "kind": "shell",
        "command": f"/bin/bash -lc {shlex.quote(body)}",
        "cwd": str(workspace),
    }, workspace=workspace)

    intent = view["semanticIntent"]
    assert intent["status"] == "verified"
    assert intent["operation"] == "test.run"
    assert intent["impact"] == "read"
    assert intent["targets"]["command"] == "python"
    assert intent["targets"]["args"] == [
        "-m", "pytest", "tests/unit/test_app.py"]


@pytest.mark.parametrize("body", [
    "docker ps --format '{{json .}}'",
    "docker inspect grafana",
    "docker logs --tail 50 grafana",
    "docker stats --no-stream",
])
def test_codex_login_shell_structures_bounded_docker_reads_as_command_read(
        tmp_path, body):
    workspace = tmp_path / "project"
    workspace.mkdir()
    view = normalize_tool_view({
        "kind": "shell",
        "command": f"/bin/zsh -lc {shlex.quote(body)}",
        "cwd": str(workspace),
    }, workspace=workspace)

    intent = view["semanticIntent"]
    assert tool_view_is_inspectable(view) is True
    assert intent["status"] == "verified"
    assert intent["operation"] == "command.read"
    assert intent["impact"] == "read"
    assert intent["targets"]["command"] == "docker"


@pytest.mark.parametrize("body", [
    "docker exec grafana sh",
    "docker restart grafana",
    "docker rm grafana",
    "docker compose up -d",
    "docker stats",
    "docker logs --follow grafana",
])
def test_codex_login_shell_keeps_docker_writes_and_ambiguous_reads_critical(
        tmp_path, body):
    workspace = tmp_path / "project"
    workspace.mkdir()
    view = normalize_tool_view({
        "kind": "shell",
        "command": f"/bin/zsh -lc {shlex.quote(body)}",
        "cwd": str(workspace),
    }, workspace=workspace)

    intent = view["semanticIntent"]
    assert tool_view_is_inspectable(view) is True
    assert intent["status"] == "verified"
    assert intent["operation"] == "command.execute"
    assert intent["impact"] == "critical"


@pytest.mark.parametrize("command", [
    "sudo /bin/zsh -lc 'docker build .'",
    "doas /bin/zsh -lc 'docker build .'",
    "env MODE=test /bin/zsh -lc 'docker build .'",
    "/usr/bin/env /bin/zsh -lc 'docker build .'",
    "/bin/zsh -lc 'sudo docker build .'",
    "/bin/zsh -lc 'doas docker build .'",
    "/bin/zsh -lc 'env MODE=test docker build .'",
    "/bin/zsh -lc 'export MODE=test'",
    "/bin/zsh -lc 'pkexec docker build .'",
    "/bin/zsh -lc 'runuser -u root docker build .'",
    "/bin/zsh -lc 'setpriv --reuid 0 docker build .'",
    "/bin/zsh -lc \"/bin/bash -lc 'docker build .'\"",
])
def test_codex_shell_privilege_environment_and_nested_wrappers_fail_closed(
        tmp_path, command):
    workspace = tmp_path / "project"
    workspace.mkdir()

    view = normalize_tool_view({
        "kind": "shell", "command": command, "cwd": str(workspace),
    }, workspace=workspace)

    assert tool_view_is_inspectable(view) is False
    assert view["semanticIntent"]["status"] == "unverified"
    assert view["semanticIntent"]["targets"]["paths"] == []


@pytest.mark.parametrize("body", [
    "docker build . | tee build.log",
    "docker build . > build.log",
    "docker build . 2>&1",
    "echo $(id)",
    "echo `id`",
    "cat <<EOF",
    "docker build . && docker push agenthub:test",
    "docker build . || true",
    "docker build .; docker push agenthub:test",
    "docker build .\ndocker push agenthub:test",
    "MODE=test docker build .",
])
def test_codex_login_shell_dangerous_or_ambiguous_body_fails_closed(
        tmp_path, body):
    workspace = tmp_path / "project"
    workspace.mkdir()

    view = normalize_tool_view({
        "kind": "shell",
        "command": f"/bin/zsh -lc {shlex.quote(body)}",
        "cwd": str(workspace),
    }, workspace=workspace)

    assert tool_view_is_inspectable(view) is False
    assert view["semanticIntent"]["status"] == "unverified"
    assert view["semanticIntent"]["targets"]["paths"] == []


def test_codex_login_shell_cwd_must_resolve_inside_workspace(tmp_path):
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escaped").symlink_to(outside, target_is_directory=True)

    for cwd in (outside, workspace / "escaped"):
        view = normalize_tool_view({
            "kind": "shell",
            "command": "/bin/zsh -lc 'docker build .'",
            "cwd": str(cwd),
        }, workspace=workspace)
        assert tool_view_is_inspectable(view) is False
        assert view["semanticIntent"]["reason"] == (
            "terminal cwd is outside the task workspace")
