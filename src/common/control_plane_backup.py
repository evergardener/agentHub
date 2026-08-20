"""Consistent control-plane backup creation and offline integrity checks."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

FORMAT_VERSION = 1
QUIESCE_SERVICES = ("state-writer", "janitor", "notifier", "orchestrator",
                    "webui", "agentgateway")
Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(RuntimeError):
    pass


MIGRATION_RECEIPT_DEST = (
    "/data/workspace/runtime/migration-backup-receipt.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _run(runner: Runner, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return runner(args, check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError) as exc:
        command = " ".join(args[:4])
        raise BackupError(f"命令失败: {command}") from exc


def _compose(runner: Runner, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return _run(runner, ["docker", "compose", *args], **kwargs)


def _write_migration_receipt(runner: Runner, writer_id: str,
                             archive: Path) -> None:
    receipt = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": _sha256(archive),
    }
    with tempfile.TemporaryDirectory(prefix="agenthub-receipt-") as temp:
        source = Path(temp) / "migration-backup-receipt.json"
        source.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        source.chmod(0o600)
        _run(runner, ["docker", "exec", writer_id, "mkdir", "-p",
                      "/data/workspace/runtime"])
        _run(runner, ["docker", "exec", writer_id, "rm", "-f",
                      f"{MIGRATION_RECEIPT_DEST}.consuming"])
        _run(runner, ["docker", "cp", str(source),
                      f"{writer_id}:{MIGRATION_RECEIPT_DEST}"])


def _write_restored_migration_receipt(agent_data: Path,
                                      archive: Path) -> None:
    destination = (agent_data / "workspace" / "runtime" /
                   "migration-backup-receipt.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": _sha256(archive),
    }, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    destination.with_suffix(".json.consuming").unlink(missing_ok=True)


def create_backup(output_dir: Path, workspace: Path | None,
                  runner: Runner = subprocess.run) -> Path:
    """Quiesce writers, copy all state, restart prior services, then archive."""
    output_dir = output_dir.expanduser().resolve()
    workspace = workspace.expanduser().resolve() if workspace else None
    if workspace and (output_dir == workspace or output_dir.is_relative_to(workspace)):
        raise BackupError("备份目录不能位于被备份 Workspace 内")
    if workspace and not workspace.is_dir():
        raise BackupError(f"Workspace 不存在或不是目录: {workspace}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)

    running_result = _compose(
        runner, "ps", "--services", "--status", "running",
        stdout=subprocess.PIPE, text=True)
    running = set(running_result.stdout.splitlines())
    required = {"postgres", "nats", "state-writer"}
    missing = sorted(required - running)
    if missing:
        raise BackupError(f"以下服务未运行: {', '.join(missing)}")
    nats_id = _compose(runner, "ps", "-aq", "nats",
                       stdout=subprocess.PIPE, text=True).stdout.strip()
    writer_id = _compose(runner, "ps", "-aq", "state-writer",
                         stdout=subprocess.PIPE, text=True).stdout.strip()
    if not nats_id or not writer_id:
        raise BackupError("无法解析 nats/state-writer 容器 ID")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = output_dir / f"agenthub-backup-{timestamp}.tar.gz"
    archive_part = output_dir / f".{archive.name}.part"
    stage = Path(tempfile.mkdtemp(prefix=".agenthub-backup-", dir=output_dir))
    stopped_apps = [name for name in QUIESCE_SERVICES if name in running]
    nats_stopped = False
    try:
        if stopped_apps:
            _compose(runner, "stop", "--timeout", "30", *stopped_apps)
        with (stage / "postgres.dump").open("wb") as dump:
            _compose(
                runner, "exec", "-T", "postgres", "pg_dump",
                "-U", "agenthub", "-d", "agenthub", "--format=custom",
                "--no-owner", "--no-acl", stdout=dump)
        _compose(runner, "stop", "--timeout", "30", "nats")
        nats_stopped = True
        (stage / "nats-data").mkdir()
        (stage / "agent-data").mkdir()
        _run(runner, ["docker", "cp", f"{nats_id}:/data/.",
                      str(stage / "nats-data")])
        _run(runner, ["docker", "cp", f"{writer_id}:/data/.",
                      str(stage / "agent-data")])
        if workspace:
            shutil.copytree(workspace, stage / "workspace", symlinks=True)

        payload = [path for path in _files(stage)
                   if path.name != "manifest.json"]
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workspace_included": workspace is not None,
            "files": {
                str(path.relative_to(stage)): {
                    "size": path.stat().st_size, "sha256": _sha256(path)}
                for path in payload
            },
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        with tarfile.open(archive_part, "w:gz") as tar:
            for child in sorted(stage.iterdir()):
                tar.add(child, arcname=child.name, recursive=True)
        archive_part.chmod(0o600)
    finally:
        restart_error: BackupError | None = None
        if nats_stopped:
            try:
                _compose(runner, "start", "nats")
            except BackupError as exc:
                restart_error = exc
        if stopped_apps:
            try:
                _compose(runner, "start", *stopped_apps)
            except BackupError as exc:
                restart_error = restart_error or exc
        shutil.rmtree(stage, ignore_errors=True)
        if restart_error:
            archive_part.unlink(missing_ok=True)
            raise BackupError("备份后恢复原运行服务失败，请立即检查 compose") \
                from restart_error
    try:
        verify_backup(archive_part)
        archive_part.replace(archive)
        try:
            _write_migration_receipt(runner, writer_id, archive)
        except BackupError as exc:
            raise BackupError(
                f"备份已验证并保留在 {archive}，但迁移回执写入失败") from exc
    except Exception:
        archive_part.unlink(missing_ok=True)
        raise
    return archive


def verify_backup(archive: Path) -> dict:
    """Safely extract and validate archive structure, sizes and checksums."""
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise BackupError(f"备份文件不存在: {archive}")
    with tempfile.TemporaryDirectory(prefix="agenthub-verify-") as temp:
        root = Path(temp)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise BackupError("备份压缩包损坏或包含不安全路径") from exc
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError("manifest.json 缺失或损坏") from exc
        if manifest.get("format_version") != FORMAT_VERSION:
            raise BackupError("不支持的备份格式版本")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise BackupError("manifest files 为空或格式错误")
        for relative, expected in files.items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise BackupError(f"备份文件缺失: {relative}")
            if path.stat().st_size != expected.get("size"):
                raise BackupError(f"备份文件大小不匹配: {relative}")
            if _sha256(path) != expected.get("sha256"):
                raise BackupError(f"备份校验和不匹配: {relative}")
        dump = root / "postgres.dump"
        if not dump.is_file() or dump.read_bytes()[:5] != b"PGDMP":
            raise BackupError("PostgreSQL custom dump 头无效")
        for required in (root / "nats-data", root / "agent-data"):
            if not required.is_dir():
                raise BackupError(f"备份缺少目录: {required.name}")
        if manifest.get("workspace_included") and not (
                root / "workspace").is_dir():
            raise BackupError("manifest 声明包含 Workspace，但目录缺失")
        return manifest


def _restore_volume(runner: Runner, service: str, source: Path) -> None:
    mount = f"{source.resolve()}:/restore:ro"
    script = ("set -eu; "
              "find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
              "cp -a /restore/. /data/")
    _compose(runner, "run", "--rm", "--no-deps", "--entrypoint", "sh",
             "-v", mount, service, "-c", script)


def restore_backup(archive: Path, safety_dir: Path,
                   workspace: Path | None,
                   runner: Runner = subprocess.run) -> dict:
    """Restore a verified archive after creating a fresh safety backup.

    Callers must enforce explicit user confirmation. On any destructive-phase
    failure services intentionally remain stopped for operator inspection.
    """
    archive = archive.expanduser().resolve()
    workspace = workspace.expanduser().resolve() if workspace else None
    if workspace:
        home = Path.home().resolve()
        if workspace in {Path("/").resolve(), home} or len(workspace.parts) < 3:
            raise BackupError("拒绝恢复到过宽的 Workspace 路径")
    verify_backup(archive)
    safety_archive = create_backup(safety_dir, workspace, runner=runner)

    with tempfile.TemporaryDirectory(prefix="agenthub-restore-") as temp:
        temp_root = Path(temp)
        stable_archive = temp_root / "restore.tar.gz"
        shutil.copy2(archive, stable_archive)
        manifest = verify_backup(stable_archive)
        extracted = temp_root / "extracted"
        extracted.mkdir()
        try:
            with tarfile.open(stable_archive, "r:gz") as tar:
                tar.extractall(extracted, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise BackupError("恢复归档解压失败") from exc

        running_result = _compose(
            runner, "ps", "--services", "--status", "running",
            stdout=subprocess.PIPE, text=True)
        running = set(running_result.stdout.splitlines())
        missing = sorted({"postgres", "nats", "state-writer"} - running)
        if missing:
            raise BackupError(f"恢复前服务状态异常: {', '.join(missing)}")
        stopped_apps = [name for name in QUIESCE_SERVICES if name in running]
        if stopped_apps:
            _compose(runner, "stop", "--timeout", "30", *stopped_apps)
        _compose(runner, "stop", "--timeout", "30", "nats")

        # Destructive phase: failures below deliberately do not restart apps.
        try:
            with (extracted / "postgres.dump").open("rb") as dump:
                _compose(
                    runner, "exec", "-T", "postgres", "pg_restore",
                    "-U", "agenthub", "-d", "agenthub", "--clean",
                    "--if-exists", "--exit-on-error", "--no-owner",
                    "--no-acl", stdin=dump)
            _restore_volume(runner, "nats", extracted / "nats-data")
            _write_restored_migration_receipt(
                extracted / "agent-data", stable_archive)
            _restore_volume(runner, "state-writer", extracted / "agent-data")

            preserved_workspace = None
            source_workspace = extracted / "workspace"
            if manifest.get("workspace_included") and workspace:
                preserved_workspace = workspace.with_name(
                    f"{workspace.name}.pre-restore-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
                if preserved_workspace.exists():
                    raise BackupError(
                        f"Workspace 保留路径已存在: {preserved_workspace}")
                if workspace.exists():
                    workspace.rename(preserved_workspace)
                try:
                    shutil.copytree(source_workspace, workspace, symlinks=True)
                except Exception:
                    shutil.rmtree(workspace, ignore_errors=True)
                    if preserved_workspace.exists():
                        preserved_workspace.rename(workspace)
                    raise
        except Exception as exc:
            raise BackupError(
                "恢复失败；控制面保持停机，请检查并使用安全备份回退") from exc

        _compose(runner, "start", "nats")
        if stopped_apps:
            _compose(runner, "start", *stopped_apps)
        return {
            "restored_from": str(archive),
            "safety_backup": str(safety_archive),
            "preserved_workspace": (
                str(preserved_workspace) if preserved_workspace else None),
        }
