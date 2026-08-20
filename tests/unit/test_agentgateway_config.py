"""agentgateway 配置的静态安全约束与本地 schema 验证。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = (
    ROOT / "infra" / "agentgateway" / "config.yaml",
    ROOT / "infra" / "agentgateway" / "config.docker.yaml",
)
AGW_BIN = ROOT / "infra" / "agentgateway" / "bin" / "agentgateway"


@pytest.mark.parametrize("config_path", CONFIGS)
def test_each_agent_route_has_an_independent_request_bucket(config_path: Path):
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    routes = config["routes"]

    assert {route["name"] for route in routes} == {
        "agent-codex",
        "agent-kimi",
        "agent-dsh",
    }
    for route in routes:
        assert route["policies"]["localRateLimit"] == [
            {
                "maxTokens": 30,
                "tokensPerFill": 30,
                "fillInterval": "60s",
                "type": "requests",
            }
        ]


@pytest.mark.skipif(not AGW_BIN.exists(), reason="local agentgateway binary not installed")
@pytest.mark.parametrize("config_path", CONFIGS)
def test_config_is_accepted_by_bundled_agentgateway(config_path: Path):
    env = dict(os.environ, GATEWAY_API_KEY="schema-validation-only-key")
    result = subprocess.run(
        [str(AGW_BIN), "--validate-only", "-f", str(config_path)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration is valid" in result.stdout
