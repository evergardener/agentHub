"""env 文件（.env）键值保障 — v3 加固。

ensure_key()：配置项缺失/为空时自动生成随机值并**原子落盘 .env**，
返回 (value, created)。created=True 表示本次初始化生成——调用方只在这时
打印日志（避免每次启动都刷敏感提示）。

设计约束（Evolution v3 M2）：
- 密钥唯一事实源是环境变量/.env，不入库、不用 Keychain；
- 多个 adapter 进程可能同时启动，追加写入用 fcntl.flock 串行化，
  后到者读到先写者的值（不会生成两个不同 token）；
- 修改 token 是安全的：双侧（adapter / hermes 容器）都从同一份 .env 读取，
  改完重启 adapter（launchctl kickstart -k）+ 重跑 agentctl 即可生效；
  切换瞬间进行中的调用会 401（无滚动双 token），单机场景可接受。
"""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from pathlib import Path
from typing import Callable


def _read_value(env_path: Path, key: str) -> str:
    """读 .env 中某键的值（朴素解析：KEY=VALUE，忽略注释/空行）。"""
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def ensure_key(env_path: Path, key: str,
               generator: Callable[[], str] | None = None) -> tuple[str, bool]:
    """确保 key 在 env_path 中有非空值；缺失则生成并追加。

    返回 (value, created)。进程环境变量里已有的同名非空值优先
    （视为运维显式覆盖，不回写文件）。
    """
    existing_env = os.environ.get(key, "").strip()
    if existing_env:
        return existing_env, False

    gen = generator or (lambda: secrets.token_hex(24))
    env_path.parent.mkdir(parents=True, exist_ok=True)

    # 追加模式下整段操作持锁：检查→生成→写入 串行化
    with open(env_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            content = f.read()
            current = ""
            for line in content.splitlines():
                line_s = line.strip()
                if line_s and not line_s.startswith("#") and "=" in line_s:
                    k, _, v = line_s.partition("=")
                    if k.strip() == key:
                        current = v.strip().strip('"').strip("'")
            if current:
                return current, False
            value = gen()
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # 密钥文件权限收紧（新建文件时）
    try:
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return value, True
