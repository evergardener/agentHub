"""Native CLI session ID parsing, resume command, and capability tests."""

from pathlib import Path

from adapters.codex.session import CodexSessionAdapter, extract_codex_session_id
from adapters.kimi.session import (
    KimiSessionAdapter,
    extract_kimi_session_id,
    find_kimi_session_for_workspace,
)


def test_codex_thread_id_and_resume_command():
    assert extract_codex_session_id(
        '{"type":"thread.started","thread_id":"codex-native-1"}\n'
        '{"type":"turn.completed"}\n') == "codex-native-1"
    adapter = CodexSessionAdapter()
    command = adapter._command(
        "/bin/codex", Path("/tmp/ws"), Path("/tmp/last.md"),
        "continue", "codex-native-1")
    assert command[:3] == ["/bin/codex", "exec", "resume"]
    assert "--json" in command
    assert command[-2:] == ["codex-native-1", "continue"]
    assert adapter.capabilities.native_resume is True
    assert adapter.capabilities.pause is False


def test_codex_new_session_is_workspace_scoped():
    adapter = CodexSessionAdapter()
    command = adapter._command(
        "/bin/codex", Path("/tmp/ws"), Path("/tmp/last.md"),
        "start", None)
    assert command[:2] == ["/bin/codex", "exec"]
    assert ["--sandbox", "workspace-write"] == command[2:4]
    assert "-C" in command and "/tmp/ws" in command


def test_kimi_session_id_and_resume_command(monkeypatch):
    assert extract_kimi_session_id(
        '{"type":"system","data":{"session_id":"kimi-native-1"}}\n'
        '{"role":"assistant","content":"done"}\n') == "kimi-native-1"
    monkeypatch.delenv("LAS_KIMI_CLI_MODEL", raising=False)
    adapter = KimiSessionAdapter()
    command = adapter._command("/bin/kimi", "continue", "kimi-native-1")
    assert command[:3] == ["/bin/kimi", "-S", "kimi-native-1"]
    assert command[-3:] == ["--output-format=stream-json", "-p", "continue"]
    assert adapter.capabilities.native_resume is True
    assert adapter.capabilities.interrupt is True


def test_session_id_parsers_fail_closed():
    assert extract_codex_session_id('{"type":"turn.completed"}') is None
    assert extract_kimi_session_id('{"role":"assistant"}') is None


def test_kimi_workspace_index_fallback(tmp_path):
    workspace = tmp_path / "task"
    workspace.mkdir()
    index = tmp_path / "session_index.jsonl"
    index.write_text(
        '{"sessionId":"old","workDir":"/other"}\n'
        + '{"sessionId":"native-from-index","workDir":"'
        + str(workspace.resolve()) + '"}\n', encoding="utf-8")
    assert find_kimi_session_for_workspace(workspace, index) == \
        "native-from-index"
