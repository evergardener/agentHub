"""Native session ID parsing and capability tests."""

from adapters.codex.session import CodexSessionAdapter, extract_codex_session_id
from adapters.kimi.session import (
    KimiSessionAdapter,
    extract_kimi_session_id,
    find_kimi_session_for_workspace,
)


def test_codex_thread_id_and_app_server_capabilities():
    assert extract_codex_session_id(
        '{"type":"thread.started","thread_id":"codex-native-1"}\n'
        '{"type":"turn.completed"}\n') == "codex-native-1"
    adapter = CodexSessionAdapter()
    assert adapter.capabilities.native_resume is True
    assert adapter.capabilities.pause is False
    assert adapter.capabilities.interactions is True


def test_kimi_session_id_and_resume_command(monkeypatch):
    assert extract_kimi_session_id(
        '{"type":"system","data":{"session_id":"kimi-native-1"}}\n'
        '{"role":"assistant","content":"done"}\n') == "kimi-native-1"
    adapter = KimiSessionAdapter()
    assert adapter.capabilities.native_resume is True
    assert adapter.capabilities.interrupt is True
    assert adapter.capabilities.interactions is True


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
