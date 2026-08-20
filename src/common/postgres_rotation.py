"""Backup-gated PostgreSQL role password rotation."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import psycopg
from psycopg import sql

from common.control_plane_backup import BackupError, verify_backup
from common.envfile import set_values
from common.preflight import parse_env


class RotationError(RuntimeError):
    pass


def _require_fresh_backup(archive: Path, max_age_seconds: int) -> dict:
    try:
        manifest = verify_backup(archive)
        created_at = datetime.fromisoformat(manifest["created_at"])
    except (BackupError, KeyError, TypeError, ValueError) as exc:
        raise RotationError(f"备份无效: {exc}") from exc
    if created_at.tzinfo is None:
        raise RotationError("备份 created_at 缺少时区")
    age = (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds()
    if age < -300 or age > max_age_seconds:
        raise RotationError(
            f"备份不在允许的新鲜度窗口内（最大 {max_age_seconds} 秒）")
    return manifest


def _write_credentials(path: Path, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) \
            if path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RotationError("凭据文件存在但无法安全解析") from exc
    if not isinstance(current, dict):
        raise RotationError("凭据文件根节点必须是 JSON 对象")
    current["postgresPassword"] = password
    rendered = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(rendered)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def rotate_password(
    env_path: Path,
    backup: Path,
    credentials_path: Path,
    *,
    max_backup_age: int = 3600,
    connector: Callable[..., object] = psycopg.connect,
    generator: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> dict[str, object]:
    """Rotate the agenthub role and persist the matching local secret."""
    manifest = _require_fresh_backup(backup, max_backup_age)
    try:
        current = parse_env(env_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RotationError(f"无法读取 .env: {exc}") from exc
    old_password = current.get("LAS_PG_PASSWORD", "")
    if not old_password:
        raise RotationError("LAS_PG_PASSWORD 未配置")
    new_password = generator()
    if len(new_password) < 24 or new_password == old_password:
        raise RotationError("密码生成器未产生合格的新密码")

    try:
        connection = connector(
            host="127.0.0.1", port=5432, dbname="agenthub",
            user="agenthub", password=old_password, autocommit=True)
    except Exception as exc:
        raise RotationError("无法用当前 .env 凭据连接 PostgreSQL") from exc

    def alter(password: str) -> None:
        query = sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier("agenthub"), sql.Literal(password))
        connection.execute(query)

    try:
        alter(new_password)
        try:
            set_values(env_path, {"LAS_PG_PASSWORD": new_password})
        except Exception as exc:
            try:
                alter(old_password)
            except Exception as rollback_exc:
                raise RotationError(
                    "写入 .env 失败且数据库密码回滚失败；立即停止部署"
                ) from rollback_exc
            raise RotationError("写入 .env 失败；数据库密码已回滚") from exc
    except RotationError:
        raise
    except Exception as exc:
        raise RotationError("PostgreSQL 密码轮换失败") from exc
    finally:
        connection.close()

    credential_warning = ""
    try:
        _write_credentials(credentials_path, new_password)
    except RotationError as exc:
        credential_warning = str(exc)
    return {
        "backupCreatedAt": manifest["created_at"],
        "credentials": str(credentials_path),
        "credentialWarning": credential_warning,
    }
