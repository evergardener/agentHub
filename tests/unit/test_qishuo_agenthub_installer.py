from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "qishuo_installer", ROOT / "scripts" / "install-qishuo-agenthub.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_install_backup_and_rollback(tmp_path):
    profile = tmp_path / "qishuo"
    profile.mkdir()
    original = {
        "agent": {"system_prompt": "original"},
        "a2a_agents": {
            "agenthub-codex": {"url": "http://old"},
            "unrelated": {"url": "http://peer"},
        },
    }
    (profile / "config.yaml").write_text(
        yaml.safe_dump(original), encoding="utf-8")
    (profile / ".env").write_text("KEEP=value\n", encoding="utf-8")
    agenthub_env = tmp_path / "agenthub.env"
    agenthub_env.write_text(
        "LAS_HERMES_GATEWAY_API_KEY=" + "t" * 48 + "\n",
        encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    appendix = tmp_path / "appendix.md"
    appendix.write_text("mandatory route", encoding="utf-8")

    result = MODULE.install(profile, agenthub_env, skill, appendix)
    backup = Path(result["backup"])
    assert result["rollback_drill"] == "passed"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert stat.S_IMODE((backup / "env.before").stat().st_mode) == 0o600
    config = yaml.safe_load((profile / "config.yaml").read_text())
    assert set(config["a2a_agents"]) == {"unrelated", "agenthub"}
    assert config["a2a_agents"]["agenthub"]["url"].endswith("/agenthub")
    assert MODULE.PROMPT_START in config["agent"]["system_prompt"]
    assert "AGENTHUB_A2A_TOKEN=" + "t" * 48 in (
        profile / ".env").read_text()

    restored = MODULE.rollback(profile, backup)
    assert restored["restored"] == str(profile)
    assert yaml.safe_load((profile / "config.yaml").read_text()) == original
    assert (profile / ".env").read_text() == "KEEP=value\n"
    assert not (profile / "skills" / "agenthub-orchestration").exists()


def test_manifest_does_not_contain_token(tmp_path):
    profile = tmp_path / "qishuo"
    profile.mkdir()
    (profile / "config.yaml").write_text("agent: {}\n", encoding="utf-8")
    token = "sensitive-" + "x" * 48
    (profile / ".env").write_text(f"OLD={token}\n", encoding="utf-8")
    backup, _ = MODULE._backup(profile)
    manifest = json.loads((backup / "manifest.json").read_text())
    assert token not in json.dumps(manifest)


def test_install_replaces_managed_prompt_block(tmp_path):
    profile = tmp_path / "qishuo"
    profile.mkdir()
    managed = (
        f"keep before\n\n{MODULE.PROMPT_START}\nold rule\n"
        f"{MODULE.PROMPT_END}\nkeep after")
    (profile / "config.yaml").write_text(
        yaml.safe_dump({"agent": {"system_prompt": managed}}),
        encoding="utf-8")
    (profile / ".env").write_text("KEEP=value\n", encoding="utf-8")
    agenthub_env = tmp_path / "agenthub.env"
    agenthub_env.write_text(
        "LAS_HERMES_GATEWAY_API_KEY=" + "t" * 48 + "\n",
        encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    appendix = tmp_path / "appendix.md"
    appendix.write_text("new task id rule", encoding="utf-8")

    MODULE.install(profile, agenthub_env, skill, appendix)
    prompt = yaml.safe_load(
        (profile / "config.yaml").read_text())["agent"]["system_prompt"]
    assert "old rule" not in prompt
    assert "new task id rule" in prompt
    assert "keep before" in prompt and "keep after" in prompt
    assert prompt.count(MODULE.PROMPT_START) == 1
    assert prompt.count(MODULE.PROMPT_END) == 1


def test_incomplete_managed_prompt_block_fails_closed():
    with pytest.raises(ValueError, match="boundary is incomplete"):
        MODULE._upsert_prompt_appendix(MODULE.PROMPT_START, "new rule")
