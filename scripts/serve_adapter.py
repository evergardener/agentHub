#!/usr/bin/env python3
"""Adapter 多地址绑定启动器（v3 加固）。

uvicorn CLI 只支持单一 --host；这里预绑定多个 socket 后交给
uvicorn.Server.serve(sockets=...)，实现同进程监听多地址：
  LAS_ADAPTER_BIND=127.0.0.1,192.168.7.10   （逗号分隔，缺省 127.0.0.1）
  LAS_ADAPTER_PORT=8201                     （缺省 8201）

用法：
  PYTHONPATH=src python scripts/serve_adapter.py codex
"""

from __future__ import annotations

import asyncio
import importlib
import os
import socket
import sys


def _bind_sockets(addrs: list[str], port: int) -> list[socket.socket]:
    """逐地址绑定；单个地址不可用（如换了网络环境）只告警跳过，
    全部失败才退出——避免笔记本换网络后 launchd 反复拉起崩溃。"""
    socks: list[socket.socket] = []
    for addr in addrs:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((addr, port))
        except OSError as exc:
            print(f"[serve_adapter] WARN: bind {addr}:{port} failed: {exc}",
                  file=sys.stderr, flush=True)
            s.close()
            continue
        s.listen(2048)
        s.setblocking(False)
        socks.append(s)
    if not socks:
        print(f"[serve_adapter] FATAL: no address bindable on port {port}",
              file=sys.stderr, flush=True)
        raise SystemExit(1)
    return socks


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    agent = sys.argv[1]

    import uvicorn

    module = importlib.import_module(f"adapters.{agent}.server")
    app = module.app

    addrs = [a.strip() for a in
             os.environ.get("LAS_ADAPTER_BIND", "127.0.0.1").split(",")
             if a.strip()]
    port = int(os.environ.get("LAS_ADAPTER_PORT", "8201"))

    socks = _bind_sockets(addrs, port)
    bound = ", ".join(f"{a}:{port}" for a in addrs)
    print(f"[serve_adapter] {agent} listening on {bound}", flush=True)

    config = uvicorn.Config(app, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve(sockets=socks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
