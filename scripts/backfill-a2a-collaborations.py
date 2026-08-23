#!/usr/bin/env python3
"""Plan/apply/rollback audited A2A Collaboration repairs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestrator.a2a_backfill import (  # noqa: E402
    APPLY_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    apply_manifest,
    plan_manifest,
    rollback_receipt,
)
from state.db import connect  # noqa: E402


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new(path: Path, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", help="explicit DB path/URL; default uses LAS_DATABASE_URL")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirmation")
    parser.add_argument("--receipt", type=Path,
                        help="new receipt path for --apply, or input for --rollback")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.rollback:
        if args.manifest or not args.receipt or args.apply:
            parser.error("--rollback requires --receipt and excludes --manifest/--apply")
    elif not args.manifest:
        parser.error("--manifest is required for dry-run/apply")
    if args.apply and not args.receipt:
        parser.error("--apply requires --receipt")
    if args.apply and args.receipt.exists():
        parser.error("receipt path already exists; refusing overwrite")

    # Deliberately do not call init_db(): even dry-run must never migrate.
    conn = connect(args.database)
    try:
        if args.rollback:
            result = rollback_receipt(
                conn, _read(args.receipt),
                confirmation=args.confirmation or "")
        else:
            manifest = _read(args.manifest)
            if args.apply:
                result = apply_manifest(
                    conn, manifest, confirmation=args.confirmation or "")
                _write_new(args.receipt, result)
            else:
                result = plan_manifest(conn, manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
