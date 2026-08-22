from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

from common.preflight import parse_env

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "peer_migration", ROOT / "scripts" / "migrate-unified-hermes-peer.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_migration_backs_up_and_replaces_worker_bound_peers(tmp_path):
    env = tmp_path / ".env"
    old_token = "o" * 48
    env.write_text("LAS_A2A_PEERS=" + json.dumps({
        old_token: {"peer": "qishuo-dsh", "worker": "dsh"},
    }) + "\nKEEP=value\n", encoding="utf-8")
    env.chmod(0o600)
    result = MODULE.migrate(env, tmp_path / "backups")
    values = parse_env(env)
    gateway_token = values["LAS_HERMES_GATEWAY_API_KEY"]
    backend_token = values["LAS_HERMES_BACKEND_TOKEN"]
    assert len(gateway_token) == len(backend_token) == 48
    assert gateway_token != backend_token
    assert json.loads(values["LAS_A2A_PEERS"]) == {
        backend_token: {"peer": "qishuo"}}
    assert values["KEEP"] == "value"
    backup = Path(result["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (backup / "agenthub.env.before").stat().st_mode) == 0o600
    manifest_text = (backup / "manifest.json").read_text()
    assert gateway_token not in manifest_text
    assert backend_token not in manifest_text

    restored = MODULE.rollback(env, backup)
    assert restored["restored"] == str(env)
    values = parse_env(env)
    assert values["KEEP"] == "value"
    assert json.loads(values["LAS_A2A_PEERS"]) == {
        old_token: {"peer": "qishuo-dsh", "worker": "dsh"}}
    assert "LAS_HERMES_GATEWAY_API_KEY" not in values
    assert "LAS_HERMES_BACKEND_TOKEN" not in values
