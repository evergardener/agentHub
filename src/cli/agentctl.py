"""agentctl — 观察与管理 CLI（设计文档 §Phase 8，Phase 0 先建骨架）。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path.home() / "AgentWorkspace" / "runtime" / "agent-state.db"


def cmd_status(db_path: Path) -> int:
    if not db_path.exists():
        print(f"state db not found: {db_path}")
        return 1
    conn = sqlite3.connect(str(db_path))
    journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
    ]
    print(f"db: {db_path}")
    print(f"journal_mode: {journal}")
    print(f"tables: {', '.join(tables)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    args = parser.parse_args()

    if args.command == "status":
        return cmd_status(args.db)
    return 2


if __name__ == "__main__":
    sys.exit(main())
