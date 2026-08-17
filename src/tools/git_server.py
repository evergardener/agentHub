"""Git MCP Server — §3.7 / Phase 7。

工具（仓库必须位于 $AGENT_WORKSPACE 根内）：
  status   git status --porcelain=v1 --branch
  diff     git diff（可指定 ref / 暂存区）
  log      git log --oneline
  commit   git add <paths> + git commit（明确列路径，不用 -A）

运行：python -m tools.git_server   （stdio 传输）
"""

from __future__ import annotations

import subprocess

from fastmcp import FastMCP

from tools import resolve_under_root

mcp = FastMCP("agent-git")

GIT_TIMEOUT = 60


def _git(repo: str, *args: str) -> str:
    repo_path = resolve_under_root(repo)
    if not (repo_path / ".git").exists():
        raise ValueError(f"not a git repo: {repo}")
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_path, capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed ({proc.returncode}): {out}")
    return out


def git_status(repo: str) -> str:
    return _git(repo, "status", "--porcelain=v1", "--branch")


def git_diff(repo: str, ref: str = "", staged: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--staged")
    if ref:
        args.append(ref)
    return _git(repo, *args)


def git_log(repo: str, limit: int = 20) -> str:
    return _git(repo, "log", "--oneline", f"-{min(limit, 100)}")


def git_commit(repo: str, message: str, paths: list[str]) -> str:
    if not paths:
        raise ValueError("paths must be explicit (no -A)")
    for rel in paths:
        # 每个路径也必须在根内，防止 ../../etc 之类逃逸
        resolve_under_root(f"{repo.rstrip('/')}/{rel}")
    _git(repo, "add", "--", *paths)
    return _git(repo, "commit", "-m", message)


@mcp.tool
def status(repo: str) -> str:
    """查看仓库状态（分支 + 变更清单）。"""
    return git_status(repo)


@mcp.tool
def diff(repo: str, ref: str = "", staged: bool = False) -> str:
    """查看差异；staged=true 看暂存区，ref 指定对比对象。"""
    return git_diff(repo, ref, staged)


@mcp.tool
def log(repo: str, limit: int = 20) -> str:
    """查看最近提交（oneline）。"""
    return git_log(repo, limit)


@mcp.tool
def commit(repo: str, message: str, paths: list[str]) -> str:
    """提交指定路径（必须显式列出，禁止全量 add）。"""
    return git_commit(repo, message, paths)


if __name__ == "__main__":
    mcp.run()
