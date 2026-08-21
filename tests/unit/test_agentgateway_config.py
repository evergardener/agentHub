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
REMOTE_CONFIG = ROOT / "infra" / "agentgateway" / "config.remote.yaml"
AGW_BIN = ROOT / "infra" / "agentgateway" / "bin" / "agentgateway"


@pytest.mark.parametrize("config_path", CONFIGS)
def test_gateway_has_separate_hermes_and_dynamic_worker_routes(config_path: Path):
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    routes = config["routes"]

    assert {route["name"] for route in routes} == {
        "agenthub-hermes-entry", "agenthub-dynamic-workers"}
    for route in routes:
        assert route["policies"]["localRateLimit"] == [
            {
                "maxTokens": 30,
                "tokensPerFill": 30,
                "fillInterval": "60s",
                "type": "requests",
            }
        ]


def test_remote_profile_requires_tls13_mtls_strict_jwt_and_claim_acl():
    config = yaml.safe_load(REMOTE_CONFIG.read_text(encoding="utf-8"))
    gateway = config["gateways"]["remote"]
    assert gateway["protocol"] == "HTTPS"
    assert gateway["tls"] == {
        "cert": "$GATEWAY_TLS_CERT_FILE",
        "key": "$GATEWAY_TLS_KEY_FILE",
        "root": "$GATEWAY_CLIENT_CA_FILE",
        "minTLSVersion": "TLS_V1_3",
        "maxTLSVersion": "TLS_V1_3",
    }
    assert gateway["jwtAuth"] == {
        "mode": "strict",
        "issuer": "$GATEWAY_JWT_ISSUER",
        "audiences": ["$GATEWAY_JWT_AUDIENCE"],
        "jwks": {"file": "$GATEWAY_JWKS_FILE"},
        "jwtValidationOptions": {
            "requiredClaims": ["exp", "nbf", "aud", "iss", "sub"],
        },
    }
    for route in config["routes"]:
        assert route["gateways"] == ["remote"]
        rule = route["policies"]["authorization"]["rules"][0]["require"]
        agent = route["name"].removeprefix("agent-")
        assert 'jwt.role == "orchestrator"' in rule
        assert f'a == "{agent}"' in rule


def test_remote_compose_is_isolated_and_mounts_identity_read_only():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.gateway-remote.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["agentgateway"]
    assert service["command"][-1].endswith("config.remote.yaml")
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["ports"] == [
        "${LAS_GATEWAY_BIND_ADDRESS:-0.0.0.0}:"
        "${LAS_GATEWAY_REMOTE_PORT:-8443}:8443"
    ]
    assert len(service["volumes"]) == 4
    assert all(volume["read_only"] is True for volume in service["volumes"])


@pytest.mark.skipif(not AGW_BIN.exists(), reason="local agentgateway binary not installed")
@pytest.mark.parametrize("config_path", CONFIGS)
def test_config_is_accepted_by_bundled_agentgateway(config_path: Path):
    env = dict(os.environ, GATEWAY_API_KEY="schema-validation-only-key",
               HERMES_GATEWAY_API_KEY="schema-validation-hermes-key")
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


@pytest.mark.skipif(not AGW_BIN.exists(), reason="local agentgateway binary not installed")
def test_remote_config_is_accepted_by_bundled_agentgateway(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-subj", "/CN=agenthub-gateway-schema-test",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    env = dict(
        os.environ,
        GATEWAY_TLS_CERT_FILE=str(cert),
        GATEWAY_TLS_KEY_FILE=str(key),
        GATEWAY_CLIENT_CA_FILE=str(cert),
        GATEWAY_JWT_ISSUER="https://issuer.example.test",
        GATEWAY_JWT_AUDIENCE="agenthub-gateway",
        GATEWAY_JWKS_FILE=str(ROOT / "tests" / "fixtures" / "empty-jwks.json"),
        AGENT_CODEX_BACKEND="127.0.0.1:8201",
        AGENT_KIMI_BACKEND="127.0.0.1:8202",
        AGENT_DSH_BACKEND="127.0.0.1:8203",
    )
    result = subprocess.run(
        [str(AGW_BIN), "--validate-only", "-f", str(REMOTE_CONFIG)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration is valid" in result.stdout
