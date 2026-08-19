"""Consistent backup creation, service recovery and offline verification."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from common.control_plane_backup import (
    BackupError, create_backup, restore_backup, verify_backup)


class FakeDocker:
    def __init__(self, fail_dump: bool = False, fail_restore: bool = False):
        self.calls: list[list[str]] = []
        self.fail_dump = fail_dump
        self.fail_restore = fail_restore
        self.restored_receipt = False

    def __call__(self, args, check, **kwargs):
        self.calls.append(args)
        if args[:5] == ["docker", "compose", "ps", "--services", "--status"]:
            return subprocess.CompletedProcess(
                args, 0, "postgres\nnats\nstate-writer\nwebui\norchestrator\n")
        if args[:4] == ["docker", "compose", "ps", "-aq"]:
            service = args[-1]
            return subprocess.CompletedProcess(args, 0, f"{service}-cid\n")
        if "pg_dump" in args:
            if self.fail_dump:
                raise subprocess.CalledProcessError(1, args)
            kwargs["stdout"].write(b"PGDMP\x01fake-custom-dump")
        if "pg_restore" in args and self.fail_restore:
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["docker", "compose", "run"] and "state-writer" in args:
            mount = args[args.index("-v") + 1].split(":/restore:ro", 1)[0]
            receipt = (Path(mount) / "workspace" / "runtime" /
                       "migration-backup-receipt.json")
            self.restored_receipt = receipt.is_file()
        if args[:2] == ["docker", "cp"]:
            if ":" not in args[-1]:
                target = Path(args[-1])
                (target / "data.bin").write_bytes(args[2].encode())
        return subprocess.CompletedProcess(args, 0, "")


def test_create_backup_quiesces_copies_restarts_and_verifies(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "artifact.md").write_text("result", encoding="utf-8")
    docker = FakeDocker()

    archive = create_backup(tmp_path / "out", workspace, runner=docker)
    manifest = verify_backup(archive)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert manifest["workspace_included"] is True
    assert "workspace/artifact.md" in manifest["files"]
    assert any(call[:3] == ["docker", "exec", "state-writer-cid"]
               for call in docker.calls)
    assert any(call[:2] == ["docker", "cp"]
               and call[-1].endswith("migration-backup-receipt.json")
               for call in docker.calls)
    assert any(call[:3] == ["docker", "compose", "stop"] for call in docker.calls)
    nats_start = docker.calls.index(["docker", "compose", "start", "nats"])
    app_start = next(i for i, call in enumerate(docker.calls)
                     if call[:3] == ["docker", "compose", "start"]
                     and call[-1] != "nats")
    assert nats_start < app_start


def test_create_backup_restarts_apps_when_dump_fails(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    docker = FakeDocker(fail_dump=True)
    with pytest.raises(BackupError, match="命令失败"):
        create_backup(tmp_path / "out", workspace, runner=docker)
    assert any(call[:3] == ["docker", "compose", "start"]
               for call in docker.calls)


def _write_archive(path: Path, payload: bytes, checksum: str | None = None):
    manifest = json.dumps({
        "format_version": 1,
        "created_at": "2026-08-20T00:00:00+00:00",
        "workspace_included": False,
        "files": {
            "postgres.dump": {
                "size": len(payload),
                "sha256": checksum or hashlib.sha256(payload).hexdigest(),
            },
        },
    }).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("manifest.json", manifest),
                           ("postgres.dump", payload)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in ("nats-data", "agent-data"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)


def test_verify_rejects_checksum_mismatch(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    _write_archive(archive, b"PGDMPpayload", checksum="0" * 64)
    with pytest.raises(BackupError, match="校验和"):
        verify_backup(archive)


def test_verify_rejects_invalid_pg_dump(tmp_path):
    archive = tmp_path / "bad-dump.tar.gz"
    _write_archive(archive, b"not-a-pg-dump")
    with pytest.raises(BackupError, match="dump 头"):
        verify_backup(archive)


def test_output_cannot_be_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(BackupError, match="不能位于"):
        create_backup(workspace / "backups", workspace, runner=FakeDocker())


def test_restore_creates_safety_backup_and_preserves_workspace(tmp_path):
    workspace = tmp_path / "AgentWorkspace"
    workspace.mkdir()
    artifact = workspace / "artifact.md"
    artifact.write_text("backup-state", encoding="utf-8")
    docker = FakeDocker()
    target = create_backup(tmp_path / "targets", workspace, runner=docker)

    artifact.write_text("current-state", encoding="utf-8")
    result = restore_backup(
        target, tmp_path / "safety", workspace, runner=docker)

    assert Path(result["safety_backup"]).is_file()
    preserved = Path(result["preserved_workspace"])
    assert preserved.is_dir()
    assert (preserved / "artifact.md").read_text() == "current-state"
    assert artifact.read_text() == "backup-state"
    restore_call = next(call for call in docker.calls if "pg_restore" in call)
    assert "--exit-on-error" in restore_call
    restore_index = docker.calls.index(restore_call)
    assert not any(call[:2] == ["docker", "exec"]
                   for call in docker.calls[restore_index + 1:])
    assert docker.restored_receipt is True


def test_restore_failure_keeps_services_stopped(tmp_path):
    workspace = tmp_path / "AgentWorkspace"
    workspace.mkdir()
    (workspace / "x").write_text("x")
    creator = FakeDocker()
    target = create_backup(tmp_path / "targets", workspace, runner=creator)
    docker = FakeDocker(fail_restore=True)

    with pytest.raises(BackupError):
        restore_backup(target, tmp_path / "safety", workspace, runner=docker)
    restore_index = next(i for i, call in enumerate(docker.calls)
                         if "pg_restore" in call)
    assert not any(call[:3] == ["docker", "compose", "start"]
                   for call in docker.calls[restore_index + 1:])


def test_restore_rejects_broad_workspace(tmp_path):
    archive = tmp_path / "valid.tar.gz"
    _write_archive(archive, b"PGDMPpayload")
    with pytest.raises(BackupError, match="过宽"):
        restore_backup(archive, tmp_path / "safety", Path.home(),
                       runner=FakeDocker())
