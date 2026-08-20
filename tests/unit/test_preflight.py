"""Production preflight rejects unsafe config without exposing values."""

from __future__ import annotations

import json

from common.preflight import (
    check_agent_catalog,
    check_production_env,
    exit_code,
    parse_env,
    render,
)


def _secure_env() -> str:
    return "\n".join([
        "LAS_LLM_API_KEY=" + "l" * 32,
        "LAS_GATEWAY_API_KEY=" + "g" * 48,
        "LAS_PG_PASSWORD=" + "p" * 32,
        "LAS_ADAPTER_TOKEN=" + "a" * 48,
        "LAS_ACTION_RECEIPT_SECRET=" + "r" * 64,
        "LAS_API_TOKEN=" + "i" * 48,
        "LAS_A2A_PEERS=",
        "LAS_PRODUCTION_MODE=true",
        "LAS_DSH_PRODUCTION_ENABLED=false",
        "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME=false",
        "LAS_WEBUI_REQUIRE_AUTH=true",
        "LAS_ORCH_REQUIRE_AUTH=true",
        "LAS_REQUIRE_MIGRATION_BACKUP=true",
        "LAS_MIGRATION_BACKUP_MAX_AGE=86400",
        "LAS_WEBUI_SESSION_SECRET=" + "s" * 64,
        "LAS_WEBUI_TOKENS=" + json.dumps({"w" * 48: "admin"}),
        "LAS_WEBUI_COOKIE_SECURE=true",
        "LAS_ALERT_WEBHOOK_URL=https://alerts.example.test/agenthub",
        "LAS_ALERT_WEBHOOK_TOKEN=" + "n" * 32,
        "",
    ])


def test_secure_env_passes(tmp_path):
    path = tmp_path / ".env"
    path.write_text(_secure_env(), encoding="utf-8")
    path.chmod(0o600)
    findings = check_production_env(path)
    assert findings == []
    assert exit_code(findings) == 0


def test_insecure_env_reports_keys_not_values(tmp_path):
    path = tmp_path / ".env"
    secret = "do-not-print-this"
    path.write_text(
        f"LAS_LLM_API_KEY={secret}\nLAS_PG_PASSWORD=agenthub-dev-only\n",
        encoding="utf-8")
    path.chmod(0o644)
    findings = check_production_env(path)
    output = render(findings)
    assert exit_code(findings) == 1
    assert "LAS_PG_PASSWORD" in output
    assert ".env" in output
    assert secret not in output


def test_loopback_cookie_is_warning_and_strict_can_fail(tmp_path):
    path = tmp_path / ".env"
    path.write_text(_secure_env().replace(
        "LAS_WEBUI_COOKIE_SECURE=true", "LAS_WEBUI_COOKIE_SECURE=false"),
        encoding="utf-8")
    path.chmod(0o600)
    findings = check_production_env(path)
    assert [item.level for item in findings] == ["warning"]
    assert exit_code(findings) == 0
    assert exit_code(findings, strict=True) == 1


def test_parser_preserves_json_and_strips_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        'export LAS_WEBUI_TOKENS={"token":"admin"} # note\n'
        'LAS_VALUE="value # retained"\n', encoding="utf-8")
    values = parse_env(path)
    assert values["LAS_WEBUI_TOKENS"] == '{"token":"admin"}'
    assert values["LAS_VALUE"] == "value # retained"


def test_remote_gateway_profile_requires_https_jwt_and_mtls_files(tmp_path):
    jwt_file = tmp_path / "gateway.jwt"
    ca_file = tmp_path / "ca.pem"
    cert_file = tmp_path / "client.pem"
    key_file = tmp_path / "client-key.pem"
    for path, content in (
        (jwt_file, "header.payload.signature"),
        (ca_file, "ca"),
        (cert_file, "cert"),
        (key_file, "key"),
    ):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)

    remote = (_secure_env()
              .replace("LAS_GATEWAY_API_KEY=" + "g" * 48,
                       "LAS_GATEWAY_API_KEY=")
              + f"LAS_GATEWAY_URL=https://gateway.example.test:8443\n"
              + f"LAS_GATEWAY_JWT_FILE={jwt_file}\n"
              + f"LAS_GATEWAY_CA_FILE={ca_file}\n"
              + f"LAS_GATEWAY_CLIENT_CERT_FILE={cert_file}\n"
              + f"LAS_GATEWAY_CLIENT_KEY_FILE={key_file}\n")
    env_file = tmp_path / ".env"
    env_file.write_text(remote, encoding="utf-8")
    env_file.chmod(0o600)
    assert check_production_env(env_file) == []


def test_remote_gateway_plaintext_and_missing_credentials_fail(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        _secure_env() + "LAS_GATEWAY_URL=http://gateway.example.test:8300\n",
        encoding="utf-8")
    env_file.chmod(0o600)
    findings = check_production_env(env_file)
    keys = {item.key for item in findings}
    assert "LAS_GATEWAY_URL" in keys
    assert {
        "LAS_GATEWAY_JWT_FILE",
        "LAS_GATEWAY_CA_FILE",
        "LAS_GATEWAY_CLIENT_CERT_FILE",
        "LAS_GATEWAY_CLIENT_KEY_FILE",
    }.issubset(keys)


def test_dsh_development_override_is_rejected_in_production(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(_secure_env().replace(
        "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME=false",
        "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME=true"), encoding="utf-8")
    env_file.chmod(0o600)
    findings = check_production_env(env_file)
    assert {item.key for item in findings} == {
        "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME"}


def test_production_mode_must_be_explicitly_enabled(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(_secure_env().replace(
        "LAS_PRODUCTION_MODE=true", "LAS_PRODUCTION_MODE=false"),
        encoding="utf-8")
    env_file.chmod(0o600)
    findings = check_production_env(env_file)
    assert {item.key for item in findings} == {"LAS_PRODUCTION_MODE"}


def test_dsh_production_route_is_blocked_until_native_enforcement(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(_secure_env().replace(
        "LAS_DSH_PRODUCTION_ENABLED=false",
        "LAS_DSH_PRODUCTION_ENABLED=true"), encoding="utf-8")
    env_file.chmod(0o600)
    findings = check_production_env(env_file)
    assert {item.key for item in findings} == {"LAS_DSH_PRODUCTION_ENABLED"}


def test_dsh_peer_route_is_blocked_until_native_enforcement(tmp_path):
    token = "d" * 48
    peers = json.dumps({token: {"peer": "reviewer", "worker": "dsh"}})
    env_file = tmp_path / ".env"
    env_file.write_text(_secure_env().replace(
        "LAS_A2A_PEERS=", f"LAS_A2A_PEERS={peers}"), encoding="utf-8")
    env_file.chmod(0o600)
    findings = check_production_env(env_file)
    assert {item.key for item in findings} == {"LAS_DSH_PRODUCTION_ENABLED"}


def test_agent_catalog_requires_dsh_static_route_disabled(tmp_path):
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(
        "agents:\n  dsh:\n    enabled: true\n    endpoint: http://dsh:8203\n",
        encoding="utf-8")
    findings = check_agent_catalog(agents_file)
    assert [item.key for item in findings] == [
        "config/agents.yaml:dsh.enabled"]


def test_production_env_audits_selected_agent_catalog(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(_secure_env(), encoding="utf-8")
    env_file.chmod(0o600)
    missing = tmp_path / "missing-agents.yaml"
    findings = check_production_env(env_file, agents_path=missing)
    assert [item.key for item in findings] == ["config/agents.yaml"]
