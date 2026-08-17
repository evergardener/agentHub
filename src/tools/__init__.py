"""共享 MCP 工具层的公共约束（§13 权限边界）。

所有文件系统/Git 工具都以 $AGENT_WORKSPACE（缺省 ~/AgentWorkspace）为根，
禁止逃逸到根目录之外。
"""

from __future__ import annotations

import os
from pathlib import Path


class PathEscapeError(PermissionError):
    """路径逃逸出允许的根目录。"""


def workspace_root() -> Path:
    from common import config as cfg

    return cfg.workspace()


def resolve_under_root(rel_path: str, root: Path | None = None) -> Path:
    """把相对路径解析到 root 之下；绝对路径必须 already 在 root 内。"""
    root = (root or workspace_root()).resolve()
    p = Path(rel_path).expanduser()
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if p != root and root not in p.parents:
        raise PathEscapeError(f"path escapes root {root}: {rel_path}")
    return p
