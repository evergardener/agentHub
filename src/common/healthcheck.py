"""Dependency-aware container health probes using only runtime dependencies."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import urllib.request


def database_ready() -> bool:
    from state.db import connect

    try:
        conn = connect()
        try:
            return conn.execute("SELECT 1;").fetchone()[0] == 1
        finally:
            conn.close()
    except Exception:
        return False


async def nats_ready() -> bool:
    import nats

    from common import config as cfg

    nc = None
    try:
        nc = await nats.connect(
            cfg.nats_url(), connect_timeout=2,
            allow_reconnect=False, max_reconnect_attempts=0)
        await nc.flush(timeout=2)
        return True
    except Exception:
        return False
    finally:
        if nc is not None:
            await nc.close()


def tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


async def _run(args: argparse.Namespace) -> bool:
    if args.probe == "database":
        return database_ready()
    if args.probe == "database-nats":
        return database_ready() and await nats_ready()
    if args.probe == "tcp":
        return tcp_ready(args.host, args.port)
    if args.probe == "http":
        return http_ready(args.url)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="probe", required=True)
    sub.add_parser("database")
    sub.add_parser("database-nats")
    tcp = sub.add_parser("tcp")
    tcp.add_argument("host")
    tcp.add_argument("port", type=int)
    http = sub.add_parser("http")
    http.add_argument("url")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(_run(args)) else 1)


if __name__ == "__main__":
    main()
