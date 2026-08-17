"""Phase 7：共享 MCP 工具层（filesystem / git / browser）。

- 核心逻辑直接测（离线）
- MCP 协议接线用 fastmcp.Client 内存传输验证
"""

from __future__ import annotations

import subprocess

import pytest

from tools import PathEscapeError
from tools.filesystem_server import (
    fs_list_dir, fs_read_file, fs_search, fs_write_file,
    mcp as fs_mcp,
)
from tools.git_server import (
    git_commit, git_diff, git_log, git_status,
    mcp as git_mcp,
)
from tools.browser_server import (
    _LinkExtractor, _TextExtractor,
    mcp as browser_mcp,
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path))
    return tmp_path


# ---------- filesystem ----------

def test_fs_roundtrip(ws):
    fs_write_file("notes/a.md", "hello agent")
    assert fs_read_file("notes/a.md") == "hello agent"
    assert "notes/" in fs_list_dir(".")
    assert "a.md" in fs_list_dir("notes")


def test_fs_search(ws):
    fs_write_file("x.py", "def foo():\n    return 42\n")
    hits = fs_search("return 42")
    assert hits and hits[0].startswith("x.py:2:")


def test_fs_escape_blocked(ws):
    with pytest.raises(PathEscapeError):
        fs_read_file("../../etc/hosts")
    with pytest.raises(PathEscapeError):
        fs_write_file("/etc/evil", "x")


# ---------- git ----------

def _init_repo(ws):
    repo = ws / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("v1\n")
    return repo


def test_git_flow(ws):
    _init_repo(ws)
    assert "## " in git_status("proj") or "No commits yet" in git_status("proj")
    out = git_commit("proj", "init", ["f.txt"])
    assert "init" in out
    assert "init" in git_log("proj")
    (ws / "proj" / "f.txt").write_text("v2\n")
    assert "+v2" in git_diff("proj")


def test_git_reject_non_repo(ws):
    (ws / "plain").mkdir()
    with pytest.raises(ValueError):
        git_status("plain")


def test_git_commit_requires_paths(ws):
    _init_repo(ws)
    with pytest.raises(ValueError):
        git_commit("proj", "x", [])


# ---------- browser（离线：仅解析器） ----------

def test_text_extractor_strips_scripts():
    ex = _TextExtractor()
    ex.feed("<html><body><p>正文</p><script>bad()</script></body></html>")
    assert ex.text() == "正文"


def test_link_extractor_absolutizes():
    ex = _LinkExtractor("https://example.com/a/b")
    ex.feed('<a href="/x">1</a><a href="c">2</a><a href="/x">3</a>')
    # 解析器不去重（去重在 fetch_links 层），只做绝对化
    assert ex.links == [
        "https://example.com/x",
        "https://example.com/a/c",
        "https://example.com/x",
    ]


# ---------- MCP 协议接线（内存传输） ----------

@pytest.mark.anyio
@pytest.mark.parametrize("server,expected", [
    (fs_mcp, {"read_file", "write_file", "list_dir", "search"}),
    (git_mcp, {"status", "diff", "log", "commit"}),
    (browser_mcp, {"text", "links"}),
])
async def test_mcp_tools_registered(server, expected):
    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == expected


@pytest.mark.anyio
async def test_mcp_call_roundtrip(ws):
    from fastmcp import Client

    async with Client(fs_mcp) as client:
        await client.call_tool("write_file",
                               {"path": "mcp.txt", "content": "via-mcp"})
        result = await client.call_tool("read_file", {"path": "mcp.txt"})
        assert "via-mcp" in result.content[0].text
