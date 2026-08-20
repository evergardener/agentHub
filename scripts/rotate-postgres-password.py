#!/usr/bin/env python3
"""Safely rotate the local agentHub PostgreSQL password."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.postgres_rotation import RotationError, rotate_password  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--credentials", type=Path,
        default=ROOT / "runtime" / "production-credentials.json")
    parser.add_argument("--max-backup-age", type=int, default=3600)
    args = parser.parse_args()
    try:
        result = rotate_password(
            args.env, args.backup, args.credentials,
            max_backup_age=args.max_backup_age)
    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("PASS: PostgreSQL password rotated; secret value was not printed")
    print(f"backup_created_at={result['backupCreatedAt']}")
    print(f"credentials={result['credentials']}")
    if result["credentialWarning"]:
        print(f"WARNING: {result['credentialWarning']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
