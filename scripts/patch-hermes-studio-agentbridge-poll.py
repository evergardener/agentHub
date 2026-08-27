#!/usr/bin/env python3
"""Install the version-pinned Hermes Studio agent-bridge wake shim.

Hermes Studio 0.6.47 stops polling its bridge after the initial recovery when
the Node process does not already know about a background delegation.  A
profile plugin can create a valid native completion after that point, but the
completion remains pending forever.  This shim makes the existing local IPC
poll run every two seconds, preserving all existing claim, routing, retry and
completion behavior.

The installer is deliberately fail-closed: it only patches the exact published
0.6.47 runtime hash and exact minified snippets.  It creates a byte-for-byte
backup before the atomic replacement and supports an exact restore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SUPPORTED_VERSION = "0.6.47"
RUNTIME_RELATIVE_PATH = Path("dist/server/index.js")
PUBLISHED_RUNTIME_SHA256 = (
    "e734a572c3ea1a080f32ae34f57eac402d45cd90b8a838526636aacf3b54bfbe"
)

ORIGINAL_TIMER = (
    b"this.backgroundPollTimer=setInterval(()=>{this.pollBackgroundWork()},500)"
)
PATCHED_TIMER = (
    b"this.backgroundPollTimer=setInterval(()=>{this.pollBackgroundWork()},2000)"
)
ORIGINAL_GATE = (
    b'needsBackgroundPoll(){if(this.closing)return!1;'
    b'if(this.backgroundRecoveryNeeded||Date.now()<'
    b'this.backgroundActivityGraceUntil)return!0;for(let e of '
    b'this.sessionMap.values())if(Object.values(e.backgroundDelegations||{})'
    b'.some(I=>I.status==="running"||I.status==="delivering")||Object.values('
    b'e.backgroundTasks||{}).some(I=>I.status==="running"&&I.runtime!=="ekko"))'
    b'return!0;return!1}'
)
PATCHED_GATE = b"needsBackgroundPoll(){return!this.closing}"


class PatchError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package_version(package_root: Path) -> str:
    package_path = package_root / "package.json"
    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot read Hermes Studio package metadata: {exc}") \
            from exc
    if payload.get("name") != "hermes-web-ui":
        raise PatchError("package root is not hermes-web-ui")
    version = str(payload.get("version") or "")
    if version != SUPPORTED_VERSION:
        raise PatchError(
            f"unsupported hermes-web-ui version {version or '<missing>'}; "
            f"expected {SUPPORTED_VERSION}")
    return version


def _runtime_state(data: bytes) -> str:
    original = (data.count(ORIGINAL_TIMER), data.count(ORIGINAL_GATE))
    patched = (data.count(PATCHED_TIMER), data.count(PATCHED_GATE))
    if original == (1, 1) and patched == (0, 0):
        return "compatible_unpatched"
    if original == (0, 0) and patched == (1, 1):
        return "patched"
    raise PatchError(
        "Hermes Studio runtime has an unknown or partial agent-bridge poll "
        f"layout: original={original}, patched={patched}")


def inspect_package(package_root: Path) -> dict[str, str]:
    package_root = package_root.resolve()
    version = _package_version(package_root)
    runtime_path = package_root / RUNTIME_RELATIVE_PATH
    try:
        data = runtime_path.read_bytes()
    except OSError as exc:
        raise PatchError(f"cannot read Hermes Studio runtime: {exc}") from exc
    return {
        "version": version,
        "runtime_path": str(runtime_path),
        "runtime_sha256": _sha256(data),
        "state": _runtime_state(data),
    }


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.agenthub-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_package_patch(
    package_root: Path,
    *,
    backup_root: Path,
    expected_original_sha256: str = PUBLISHED_RUNTIME_SHA256,
) -> dict[str, str]:
    package_root = package_root.resolve()
    inspection = inspect_package(package_root)
    if inspection["state"] == "patched":
        return {**inspection, "status": "already_applied"}
    if inspection["runtime_sha256"] != expected_original_sha256:
        raise PatchError(
            "Hermes Studio runtime SHA-256 does not match the supported "
            f"published build: got {inspection['runtime_sha256']}, expected "
            f"{expected_original_sha256}")

    runtime_path = Path(inspection["runtime_path"])
    original = runtime_path.read_bytes()
    patched = original.replace(ORIGINAL_TIMER, PATCHED_TIMER).replace(
        ORIGINAL_GATE, PATCHED_GATE)
    if _runtime_state(patched) != "patched":
        raise PatchError("post-patch runtime verification failed")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = backup_root.resolve() / (
        f"hermes-web-ui-{SUPPORTED_VERSION}-{stamp}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / "index.js"
    shutil.copy2(runtime_path, backup_path)
    if _sha256(backup_path.read_bytes()) != inspection["runtime_sha256"]:
        raise PatchError("backup verification failed; runtime was not modified")

    _atomic_write(runtime_path, patched, runtime_path.stat().st_mode)
    after = inspect_package(package_root)
    if after["state"] != "patched":
        raise PatchError("installed runtime verification failed")
    return {**after, "status": "applied", "backup_path": str(backup_path)}


def restore_package_patch(package_root: Path, backup_path: Path) -> dict[str, str]:
    package_root = package_root.resolve()
    inspection = inspect_package(package_root)
    if inspection["state"] != "patched":
        raise PatchError("restore requires the supported patched runtime")
    runtime_path = Path(inspection["runtime_path"])
    try:
        original = backup_path.resolve(strict=True).read_bytes()
    except OSError as exc:
        raise PatchError(f"cannot read runtime backup: {exc}") from exc
    if _runtime_state(original) != "compatible_unpatched":
        raise PatchError("backup is not the supported unpatched runtime")
    _atomic_write(runtime_path, original, runtime_path.stat().st_mode)
    after = inspect_package(package_root)
    return {**after, "status": "restored"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package-root", type=Path,
        default=Path("/opt/homebrew/lib/node_modules/hermes-web-ui"))
    parser.add_argument(
        "--backup-root", type=Path,
        default=Path.home() / ".hermes-web-ui" / "backups" /
        "agenthub-agentbridge-poll")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore", type=Path)
    args = parser.parse_args()
    try:
        if args.apply:
            result = apply_package_patch(
                args.package_root, backup_root=args.backup_root)
        elif args.restore:
            result = restore_package_patch(args.package_root, args.restore)
        else:
            result = inspect_package(args.package_root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except PatchError as exc:
        print(json.dumps({"status": "error", "error": str(exc)},
                         ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
