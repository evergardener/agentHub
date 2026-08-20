"""Production preflight rejects unsafe config without exposing values."""

from __future__ import annotations

import json

from common.preflight import check_production_env, exit_code, parse_env, render


def _secure_env() -> str:
    return "\n".join([
        "LAS_LLM_API_KEY=" + "l" * 32,
        "LAS_GATEWAY_API_KEY=" + "g" * 48,
        "LAS_PG_PASSWORD=" + "p" * 32,
        "LAS_ADAPTER_TOKEN=" + "a" * 48,
        "LAS_ACTION_RECEIPT_SECRET=" + "r" * 64,
        "LAS_API_TOKEN=" + "i" * 48,
        "LAS_A2A_PEERS=",
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
