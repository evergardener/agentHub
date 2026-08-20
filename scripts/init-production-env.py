#!/usr/bin/env python3
"""Initialize the local-loopback production profile without printing secrets."""

from __future__ import annotations

import argparse
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

from common.envfile import set_values  # noqa: E402
from common.preflight import parse_env  # noqa: E402


def _token(bytes_count: int) -> str:
    return secrets.token_hex(bytes_count)


def _valid_token_map(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        token: role for token, role in value.items()
        if isinstance(token, str) and len(token) >= 24
        and role in {"viewer", "operator", "admin"}
    }


def _valid_peers(raw: str) -> dict[str, dict[str, str]]:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        token: meta for token, meta in value.items()
        if isinstance(token, str) and len(token) >= 24
        and isinstance(meta, dict)
        and meta.get("worker") in {"codex", "dsh"}
        and isinstance(meta.get("peer"), str) and meta["peer"]
    }


def initialize(
    env_path: Path,
    *,
    credentials_path: Path,
    backup_dir: Path,
) -> dict[str, object]:
    env_path.touch(mode=0o600, exist_ok=True)
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    current = parse_env(env_path)

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{env_path.name}.{stamp}"
    shutil.copyfile(env_path, backup_path)
    os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)

    web_tokens = _valid_token_map(current.get("LAS_WEBUI_TOKENS", ""))
    admin_token = next((
        token for token, role in web_tokens.items() if role == "admin"
    ), None) or _token(24)
    web_tokens[admin_token] = "admin"

    peers = _valid_peers(current.get("LAS_A2A_PEERS", ""))
    dsh_token = next((
        token for token, meta in peers.items() if meta["worker"] == "dsh"
    ), None) or _token(24)
    peers[dsh_token] = {"peer": "qishuo-dsh", "worker": "dsh"}

    def existing_or(key: str, bytes_count: int) -> str:
        value = current.get(key, "")
        return value if len(value) >= bytes_count * 2 else _token(bytes_count)

    updates = {
        "LAS_PRODUCTION_MODE": "true",
        "LAS_WEBUI_REQUIRE_AUTH": "true",
        "LAS_ORCH_REQUIRE_AUTH": "true",
        "LAS_REQUIRE_MIGRATION_BACKUP": "true",
        "LAS_MIGRATION_BACKUP_MAX_AGE": "86400",
        "LAS_WEBUI_COOKIE_SECURE": "false",
        "LAS_DSH_WEB_URL": "http://127.0.0.1:3080",
        "LAS_DSH_PRODUCTION_ENABLED": "true",
        "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME": "false",
        "LAS_DSH_PERMISSION_PRESET": "read-only",
        "LAS_DSH_AGENT_PRESET": "standard",
        "LAS_KIMI_PRODUCTION_ENABLED": "false",
        "LAS_GATEWAY_API_KEY": existing_or("LAS_GATEWAY_API_KEY", 24),
        "LAS_ADAPTER_TOKEN": existing_or("LAS_ADAPTER_TOKEN", 24),
        "LAS_ACTION_RECEIPT_SECRET": existing_or(
            "LAS_ACTION_RECEIPT_SECRET", 32),
        "LAS_WEBUI_SESSION_SECRET": existing_or(
            "LAS_WEBUI_SESSION_SECRET", 32),
        "LAS_WEBUI_TOKENS": json.dumps(
            web_tokens, separators=(",", ":"), sort_keys=True),
        "LAS_A2A_PEERS": json.dumps(
            peers, separators=(",", ":"), sort_keys=True),
    }
    set_values(env_path, updates)

    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_text(json.dumps({
        "webuiAdminToken": admin_token,
        "dshPeerToken": dsh_token,
        "webuiUrl": "http://127.0.0.1:18070",
        "note": "owner-readable bootstrap credentials; move to your secret store",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(credentials_path, stat.S_IRUSR | stat.S_IWUSR)
    return {
        "env": str(env_path),
        "backup": str(backup_path),
        "credentials": str(credentials_path),
        "postgresPasswordRotated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--credentials", type=Path,
        default=ROOT / "runtime" / "production-credentials.json")
    parser.add_argument(
        "--backup-dir", type=Path,
        default=ROOT / "runtime" / "env-backups")
    args = parser.parse_args()
    result = initialize(
        args.env, credentials_path=args.credentials,
        backup_dir=args.backup_dir)
    print("PASS: local-loopback production profile initialized")
    print(f"env={result['env']}")
    print(f"backup={result['backup']}")
    print(f"credentials={result['credentials']}")
    print("postgres_password_rotated=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
