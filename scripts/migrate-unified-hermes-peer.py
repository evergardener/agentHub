#!/usr/bin/env python3
"""Migrate agentHub secrets to one qishuo caller without printing tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.envfile import set_values
from common.preflight import parse_env


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate(env_path: Path, backup_root: Path) -> dict[str, str]:
    current = parse_env(env_path)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_root / f"unified-hermes-{stamp}"
    backup.mkdir(mode=0o700)
    before = backup / "agenthub.env.before"
    shutil.copyfile(env_path, before)
    os.chmod(before, 0o600)

    token = current.get("LAS_HERMES_GATEWAY_API_KEY", "")
    if len(token) < 48:
        token = secrets.token_hex(24)
    backend_token = current.get("LAS_HERMES_BACKEND_TOKEN", "")
    if len(backend_token) < 48 or backend_token == token:
        backend_token = secrets.token_hex(24)
    peers = json.dumps(
        {backend_token: {"peer": "qishuo"}},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    set_values(env_path, {
        "LAS_HERMES_GATEWAY_API_KEY": token,
        "LAS_HERMES_BACKEND_TOKEN": backend_token,
        "LAS_A2A_PEERS": peers,
    })
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

    manifest = {
        "version": 1,
        "created_at": stamp,
        "source": str(env_path),
        "backup": str(before),
        "sha256": _sha256(before),
        "rollback": "stop gateway/orchestrator, restore backup over source, restart",
    }
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return {"backup": str(backup), "manifest": str(manifest_path)}


def rollback(env_path: Path, backup: Path) -> dict[str, str]:
    manifest = json.loads((backup / "manifest.json").read_text())
    if Path(manifest["source"]).resolve() != env_path.resolve():
        raise ValueError("backup belongs to another environment file")
    source = Path(manifest["backup"])
    if not source.is_absolute():
        source = backup / source.name
    if _sha256(source) != manifest["sha256"]:
        raise ValueError("backup checksum mismatch")
    shutil.copyfile(source, env_path)
    os.chmod(env_path, 0o600)
    return {"restored": str(env_path), "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if args.rollback:
        result = rollback(args.env, args.rollback)
    else:
        if not args.backup_root:
            parser.error("migration requires --backup-root")
        result = migrate(args.env, args.backup_root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
