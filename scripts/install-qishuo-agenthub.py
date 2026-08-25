#!/usr/bin/env python3
"""Install or roll back qishuo's profile-local agentHub integration safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from common.envfile import set_values
from common.preflight import parse_env

PROMPT_START = "<!-- AGENTHUB-DELEGATION-BOUNDARY:START -->"
PROMPT_END = "<!-- AGENTHUB-DELEGATION-BOUNDARY:END -->"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash a directory tree without depending on filesystem traversal order."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_hash = bytes.fromhex(_sha256(path))
        digest.update(len(file_hash).to_bytes(8, "big"))
        digest.update(file_hash)
    return digest.hexdigest()


def _copy_secure(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)


def _backup(profile: Path) -> tuple[Path, dict]:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = profile / "backups" / f"agenthub-unified-{stamp}"
    target.mkdir(parents=True, mode=0o700)
    os.chmod(target, 0o700)
    entries: dict[str, dict] = {}
    for name in ("config.yaml", ".env"):
        source = profile / name
        backup = target / f"{name.removeprefix('.')}.before"
        _copy_secure(source, backup)
        entries[name] = {"existed": True, "sha256": _sha256(backup),
                         "backup": backup.name}
    skill = profile / "skills" / "agenthub-orchestration"
    if skill.exists():
        saved = target / "skill.before"
        shutil.copytree(skill, saved)
        for path in saved.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
        entries["skill"] = {
            "existed": True,
            "sha256": _tree_sha256(saved),
        }
    else:
        entries["skill"] = {"existed": False}
    supervisor_plugin = profile / "plugins" / "agenthub-supervisor"
    if supervisor_plugin.exists():
        saved = target / "supervisor-plugin.before"
        shutil.copytree(supervisor_plugin, saved)
        for path in saved.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
        entries["supervisor_plugin"] = {
            "existed": True,
            "sha256": _tree_sha256(saved),
        }
    else:
        entries["supervisor_plugin"] = {"existed": False}
    manifest = {"version": 1, "profile": str(profile), "entries": entries}
    manifest_path = target / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return target, manifest


def _verify_restore_drill(backup: Path, manifest: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="qishuo-rollback-drill-") as raw:
        drill = Path(raw)
        for name in ("config.yaml", ".env"):
            entry = manifest["entries"][name]
            restored = drill / name
            shutil.copyfile(backup / entry["backup"], restored)
            if _sha256(restored) != entry["sha256"]:
                raise RuntimeError(f"rollback drill checksum failed: {name}")
        for entry_name, backup_name in (
                ("skill", "skill.before"),
                ("supervisor_plugin", "supervisor-plugin.before")):
            entry = manifest["entries"].get(entry_name, {"existed": False})
            if not entry["existed"]:
                continue
            restored = drill / backup_name
            shutil.copytree(backup / backup_name, restored)
            if _tree_sha256(restored) != entry["sha256"]:
                raise RuntimeError(
                    f"rollback drill checksum failed: {entry_name}")


def _upsert_prompt_appendix(existing: str, appendix: str) -> str:
    """Insert or replace exactly one managed prompt block."""
    has_start = PROMPT_START in existing
    has_end = PROMPT_END in existing
    if has_start != has_end:
        raise ValueError("agentHub prompt boundary is incomplete")
    if existing.count(PROMPT_START) > 1 or existing.count(PROMPT_END) > 1:
        raise ValueError("multiple agentHub prompt boundaries found")
    block = f"{PROMPT_START}\n{appendix.strip()}\n{PROMPT_END}"
    if not has_start:
        return f"{existing.rstrip()}\n\n{block}\n"
    before, remainder = existing.split(PROMPT_START, 1)
    _, after = remainder.split(PROMPT_END, 1)
    return f"{before.rstrip()}\n\n{block}{after}"


def install(profile: Path, agenthub_env: Path, skill_source: Path,
            prompt_appendix: Path,
            supervisor_plugin_source: Path | None = None) -> dict[str, str]:
    backup, manifest = _backup(profile)
    _verify_restore_drill(backup, manifest)

    token = parse_env(agenthub_env).get("LAS_HERMES_GATEWAY_API_KEY", "")
    if len(token) < 24:
        raise ValueError("agentHub Hermes gateway token is missing or weak")
    config_path = profile / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    peers = config.get("a2a_agents") or {}
    peers = {name: value for name, value in peers.items()
             if not name.startswith("agenthub-") and name != "agenthub"}
    peers["agenthub"] = {
        "url": "http://127.0.0.1:8300/agenthub",
        "auth": {"type": "bearer", "token": "${AGENTHUB_A2A_TOKEN}"},
        "timeout": 900,
        "capabilities": [
            "orchestration", "registry", "approvals", "artifacts"],
    }
    config["a2a_agents"] = peers

    if supervisor_plugin_source is not None:
        required = {
            "plugin.yaml": supervisor_plugin_source / "plugin.yaml",
            "__init__.py": supervisor_plugin_source / "__init__.py",
        }
        missing = [name for name, path in required.items() if not path.is_file()]
        if missing:
            raise ValueError(
                f"agenthub-supervisor plugin missing: {', '.join(missing)}")
        plugins = config.setdefault("plugins", {})
        enabled = list(plugins.get("enabled") or [])
        if "agenthub-supervisor" not in enabled:
            enabled.append("agenthub-supervisor")
        plugins["enabled"] = enabled
        entries = plugins.setdefault("entries", {})
        plugin_entry = entries.setdefault("agenthub-supervisor", {})
        plugin_entry["allow_gateway_injection"] = True

    agent = config.setdefault("agent", {})
    existing_prompt = str(agent.get("system_prompt") or "").rstrip()
    appendix = prompt_appendix.read_text(encoding="utf-8").strip()
    agent["system_prompt"] = _upsert_prompt_appendix(existing_prompt, appendix)
    rendered = yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False, width=100)
    config_path.write_text(rendered, encoding="utf-8")
    os.chmod(config_path, 0o600)

    profile_env = profile / ".env"
    set_values(profile_env, {"AGENTHUB_A2A_TOKEN": token})
    os.chmod(profile_env, 0o600)

    skill_target = profile / "skills" / "agenthub-orchestration"
    skill_target.parent.mkdir(parents=True, exist_ok=True)
    if skill_target.exists():
        shutil.rmtree(skill_target)
    shutil.copytree(skill_source, skill_target)
    if supervisor_plugin_source is not None:
        plugin_target = profile / "plugins" / "agenthub-supervisor"
        plugin_target.parent.mkdir(parents=True, exist_ok=True)
        if plugin_target.exists():
            shutil.rmtree(plugin_target)
        shutil.copytree(supervisor_plugin_source, plugin_target)
    return {"backup": str(backup), "rollback_drill": "passed"}


def rollback(profile: Path, backup: Path) -> dict[str, str]:
    manifest = json.loads((backup / "manifest.json").read_text())
    if Path(manifest["profile"]).resolve() != profile.resolve():
        raise ValueError("backup belongs to another profile")
    for name in ("config.yaml", ".env"):
        entry = manifest["entries"][name]
        source = backup / entry["backup"]
        if _sha256(source) != entry["sha256"]:
            raise ValueError(f"backup checksum mismatch: {name}")
        _copy_secure(source, profile / name)
    skill_target = profile / "skills" / "agenthub-orchestration"
    skill_target.parent.mkdir(parents=True, exist_ok=True)
    if skill_target.exists():
        shutil.rmtree(skill_target)
    if manifest["entries"]["skill"]["existed"]:
        skill_backup = backup / "skill.before"
        if _tree_sha256(skill_backup) != manifest["entries"]["skill"][
                "sha256"]:
            raise ValueError("backup checksum mismatch: skill")
        shutil.copytree(skill_backup, skill_target)
    plugin_target = profile / "plugins" / "agenthub-supervisor"
    plugin_target.parent.mkdir(parents=True, exist_ok=True)
    if plugin_target.exists():
        shutil.rmtree(plugin_target)
    plugin_entry = manifest["entries"].get(
        "supervisor_plugin", {"existed": False})
    if plugin_entry["existed"]:
        plugin_backup = backup / "supervisor-plugin.before"
        if _tree_sha256(plugin_backup) != plugin_entry["sha256"]:
            raise ValueError("backup checksum mismatch: supervisor_plugin")
        shutil.copytree(plugin_backup, plugin_target)
    return {"restored": str(profile), "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--agenthub-env", type=Path)
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--prompt-appendix", type=Path)
    parser.add_argument("--supervisor-plugin-source", type=Path)
    args = parser.parse_args()
    if args.rollback:
        result = rollback(args.profile, args.rollback)
    else:
        if not all((args.agenthub_env, args.skill_source,
                    args.prompt_appendix)):
            parser.error("install requires agenthub env, skill source and appendix")
        result = install(args.profile, args.agenthub_env, args.skill_source,
                         args.prompt_appendix,
                         supervisor_plugin_source=args.supervisor_plugin_source)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
