"""Artifact manifest semantics for the WebUI task detail."""

from __future__ import annotations

from webui.server import _artifact_availability, _is_deliverable_artifact


def test_adapter_records_are_not_user_deliverables(tmp_path):
    history = tmp_path / "dsh-history.json"
    report = tmp_path / "last-message.md"
    history.write_text("{}", encoding="utf-8")
    report.write_text("agent response", encoding="utf-8")

    for name, path in (("dsh-history.json", history),
                       ("last-message.md", report)):
        manifest = _artifact_availability(
            {"name": name, "type": "log", "path": str(path)},
            [tmp_path],
        )
        assert manifest["is_deliverable"] is False
        assert manifest["available"] is True


def test_workspace_file_remains_a_deliverable_even_for_reserved_basename(
        tmp_path):
    artifact = {"name": "workspace/last-message.md", "type": "file",
                "path": str(tmp_path / "last-message.md")}
    assert _is_deliverable_artifact(artifact) is True


def test_non_reserved_artifact_is_a_deliverable(tmp_path):
    artifact = {"name": "workspace/report.md", "type": "file",
                "path": str(tmp_path / "report.md")}
    assert _is_deliverable_artifact(artifact) is True
