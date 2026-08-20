"""PostgreSQL password rotation safety tests."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.postgres_rotation import RotationError, rotate_password


class FakeConnection:
    def __init__(self, *, fail_on_execute: int | None = None):
        self.passwords: list[str] = []
        self.execute_count = 0
        self.fail_on_execute = fail_on_execute
        self.closed = False

    def execute(self, query):
        self.execute_count += 1
        if self.execute_count == self.fail_on_execute:
            raise RuntimeError("database failure")
        self.passwords.append(query.as_string().rsplit(" ", 1)[-1].strip("'"))

    def close(self):
        self.closed = True


def _archive(path: Path, *, age: timedelta = timedelta()) -> Path:
    dump = b"PGDMPpayload"
    manifest = json.dumps({
        "format_version": 1,
        "created_at": (datetime.now(UTC) - age).isoformat(),
        "workspace_included": False,
        "files": {"postgres.dump": {
            "size": len(dump), "sha256": hashlib.sha256(dump).hexdigest(),
        }},
    }).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("manifest.json", manifest), ("postgres.dump", dump)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in ("nats-data", "agent-data"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
    return path


def test_rotation_updates_database_env_and_owner_credentials(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LAS_PG_PASSWORD=old-password\n", encoding="utf-8")
    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"webuiAdminToken":"keep"}\n', encoding="utf-8")
    connection = FakeConnection()
    seen: dict = {}

    def connect(**kwargs):
        seen.update(kwargs)
        return connection

    result = rotate_password(
        env, _archive(tmp_path / "backup.tar.gz"), credentials,
        connector=connect, generator=lambda: "N" * 32)

    assert seen["password"] == "old-password"
    assert connection.passwords == ["N" * 32]
    assert connection.closed is True
    assert "LAS_PG_PASSWORD=" + "N" * 32 in env.read_text()
    assert json.loads(credentials.read_text())["postgresPassword"] == "N" * 32
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert result["credentialWarning"] == ""


def test_rotation_rejects_stale_backup_before_database_connect(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LAS_PG_PASSWORD=old-password\n", encoding="utf-8")

    with pytest.raises(RotationError, match="新鲜度"):
        rotate_password(
            env,
            _archive(tmp_path / "backup.tar.gz", age=timedelta(hours=2)),
            tmp_path / "credentials.json",
            connector=lambda **kwargs: pytest.fail("must not connect"),
            generator=lambda: "N" * 32)


def test_rotation_does_not_change_env_when_database_rejects(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LAS_PG_PASSWORD=old-password\n", encoding="utf-8")
    connection = FakeConnection(fail_on_execute=1)

    with pytest.raises(RotationError, match="轮换失败"):
        rotate_password(
            env, _archive(tmp_path / "backup.tar.gz"),
            tmp_path / "credentials.json",
            connector=lambda **kwargs: connection,
            generator=lambda: "N" * 32)

    assert env.read_text() == "LAS_PG_PASSWORD=old-password\n"
    assert connection.closed is True
