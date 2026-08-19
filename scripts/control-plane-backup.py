#!/usr/bin/env python3
"""Create or verify an agentHub control-plane backup archive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from common.control_plane_backup import (  # noqa: E402
    BackupError, create_backup, verify_backup)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--output", type=Path, default=ROOT / "backups")
    create.add_argument("--workspace", type=Path,
                        default=Path.home() / "AgentWorkspace")
    create.add_argument("--skip-workspace", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("archive", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            archive = create_backup(
                args.output, None if args.skip_workspace else args.workspace)
            print(f"PASS: backup created and verified: {archive}")
        else:
            manifest = verify_backup(args.archive)
            print("PASS: backup verified: "
                  f"{len(manifest['files'])} files, "
                  f"created_at={manifest['created_at']}")
        return 0
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
