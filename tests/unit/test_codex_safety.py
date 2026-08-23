from adapters.codex.safety import normalize_tool_view


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
