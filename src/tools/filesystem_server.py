"""Filesystem MCP Server — §3.7 / Phase 7。

工具（全部限制在 $AGENT_WORKSPACE 根内）：
  read_file   读取文本文件
  write_file  写入文本文件（自动建父目录）
  list_dir    列目录
  search      按子串搜索文件内容

运行：python -m tools.filesystem_server   （stdio 传输）
"""

from __future__ import annotations

from fastmcp import FastMCP

from tools import resolve_under_root, workspace_root

mcp = FastMCP("agent-filesystem")

MAX_READ_BYTES = 1_000_000


def fs_read_file(path: str) -> str:
    p = resolve_under_root(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")


def fs_write_file(path: str, content: str) -> str:
    p = resolve_under_root(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {p}"


def fs_list_dir(path: str = ".") -> list[str]:
    p = resolve_under_root(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return sorted(
        c.name + ("/" if c.is_dir() else "") for c in p.iterdir()
    )


def fs_search(pattern: str, path: str = ".", max_results: int = 50) -> list[str]:
    root = resolve_under_root(path)
    hits: list[str] = []
    for f in sorted(root.rglob("*")):
        if len(hits) >= max_results:
            break
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern in line:
                hits.append(f"{f.relative_to(workspace_root())}:{i}: {line.strip()[:200]}")
                break
    return hits


@mcp.tool(annotations={"readOnlyHint": True})
def read_file(path: str) -> str:
    """读取工作区内文本文件（相对 $AGENT_WORKSPACE）。"""
    return fs_read_file(path)


@mcp.tool
def write_file(path: str, content: str) -> str:
    """写入工作区内文本文件，自动创建父目录。"""
    return fs_write_file(path, content)


@mcp.tool(annotations={"readOnlyHint": True})
def list_dir(path: str = ".") -> list[str]:
    """列工作区内目录；目录名带 / 后缀。"""
    return fs_list_dir(path)


@mcp.tool(annotations={"readOnlyHint": True})
def search(pattern: str, path: str = ".", max_results: int = 50) -> list[str]:
    """在工作区内按子串搜索文件内容，返回 文件:行号: 内容。"""
    return fs_search(pattern, path, max_results)


if __name__ == "__main__":
    mcp.run()
