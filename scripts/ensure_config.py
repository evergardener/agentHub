#!/usr/bin/env python3
"""ensure_config.py — 确保配置项存在于 .env（缺失则随机生成落盘）。

用法: ensure_config.py <KEY> [env_file]
  stdout: 配置项的值（供调用方捕获 export）
  stderr: 仅首次初始化生成时打印提示（落 launchd/启动日志）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from common.envfile import ensure_key  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    key = sys.argv[1]
    env_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".env")

    value, created = ensure_key(env_file, key)
    if created:
        print(f"[ensure_config] {key} 未配置，已生成随机值并写入 {env_file} "
              f"（仅此一次提示；修改该值后重启服务即生效）",
              file=sys.stderr, flush=True)
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
