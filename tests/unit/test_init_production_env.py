"""Local-loopback production environment bootstrap tests."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

from common.preflight import parse_env


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "init_production_env", ROOT / "scripts" / "init-production-env.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_initializer_migrates_worker_peers_to_one_hub_peer(tmp_path):
    env = tmp_path / ".env"
    codex_token = "c" * 48
    kimi_token = "k" * 48
    env.write_text(
        "LAS_GATEWAY_API_KEY=" + "g" * 48 + "\n"
        "LAS_ADAPTER_TOKEN=" + "a" * 48 + "\n"
        "LAS_PG_PASSWORD=agenthub-dev-only\n"
        "LAS_A2A_PEERS=" + json.dumps({
            codex_token: {"peer": "qishuo-codex", "worker": "codex"},
            kimi_token: {"peer": "qishuo-kimi", "worker": "kimi"},
        }) + "\n",
        encoding="utf-8")
    credentials = tmp_path / "runtime" / "credentials.json"
    result = MODULE.initialize(
        env, credentials_path=credentials,
        backup_dir=tmp_path / "runtime" / "backups")

    values = parse_env(env)
    peers = json.loads(values["LAS_A2A_PEERS"])
    assert len(peers) == 1
    assert list(peers.values()) == [{"peer": "qishuo"}]
    assert values["LAS_HERMES_GATEWAY_API_KEY"] in peers
    assert values["LAS_DSH_AGENT_PRESET"] == "standard"
    assert values["LAS_DSH_PERMISSION_PRESET"] == "read-only"
    assert values["LAS_KIMI_PRODUCTION_ENABLED"] == "false"
    assert values["LAS_PG_PASSWORD"] == "agenthub-dev-only"
    assert len(values["LAS_ACTION_RECEIPT_SECRET"]) >= 64
    assert len(values["LAS_WEBUI_SESSION_SECRET"]) >= 64
    assert json.loads(values["LAS_WEBUI_TOKENS"])
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    saved = json.loads(credentials.read_text(encoding="utf-8"))
    assert saved["webuiAdminToken"] in json.loads(
        values["LAS_WEBUI_TOKENS"])
    assert saved["agenthubPeerToken"] in peers
    assert Path(str(result["backup"])).is_file()
