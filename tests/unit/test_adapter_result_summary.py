from __future__ import annotations

from pathlib import Path

from adapters.server_common import (
    MAX_RESULT_SUMMARY_CHARS,
    _result_summary,
)


def test_result_summary_uses_canonical_last_message(tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    report = tmp_path / "tasks" / "T-1" / "artifacts" / "last-message.md"
    report.parent.mkdir(parents=True)
    report.write_text("real worker answer", encoding="utf-8")

    assert _result_summary(
        "dsh", [{"name": "last-message.md", "path": str(report)}]
    ) == "real worker answer"


def test_result_summary_is_bounded_and_rejects_outside_path(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path / "workspace"))
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    assert _result_summary(
        "codex", [{"name": "last-message.md", "path": str(outside)}]
    ) == "codex completed (see artifacts)"

    report = (tmp_path / "workspace" / "tasks" / "T-2" / "artifacts"
              / "last-message.md")
    report.parent.mkdir(parents=True)
    report.write_text("x" * (MAX_RESULT_SUMMARY_CHARS + 10), encoding="utf-8")
    summary = _result_summary(
        "codex", [{"name": "last-message.md", "path": str(report)}])
    assert summary.endswith("\n…")
    assert len(summary) == MAX_RESULT_SUMMARY_CHARS + 2
