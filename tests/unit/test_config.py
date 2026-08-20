"""common.config 单元测试：LAS_* 正式名优先、旧别名兜底、默认值。"""

from __future__ import annotations

import pytest

from common import config as cfg

ALL_KEYS = [
    "LAS_WORKSPACE", "AGENT_WORKSPACE",
    "LAS_STATE_DB", "AGENT_STATE_DB",
    "LAS_NATS_URL", "NATS_URL",
    "LAS_GATEWAY_URL", "AGENT_GATEWAY_URL",
    "LAS_GATEWAY_API_KEY", "GATEWAY_API_KEY",
    "LAS_GATEWAY_JWT_FILE", "LAS_GATEWAY_CA_FILE",
    "LAS_GATEWAY_CLIENT_CERT_FILE", "LAS_GATEWAY_CLIENT_KEY_FILE",
    "LAS_LLM_BASE_URL", "KIMI_API_BASE",
    "LAS_LLM_API_KEY", "CLIPROXY_API_KEY",
    "LAS_LLM_MODEL", "KIMI_MODEL",
    "LAS_HINDSIGHT_URL", "HINDSIGHT_API_URL",
    "LAS_HINDSIGHT_API_KEY", "HINDSIGHT_API_KEY",
    "LAS_WEBUI_TOKENS", "LAS_WEBUI_SESSION_SECRET",
    "LAS_WEBUI_SESSION_TTL", "LAS_WEBUI_COOKIE_SECURE",
    "LAS_WEBUI_REQUIRE_AUTH",
    "LAS_ORCH_REQUIRE_AUTH",
    "LAS_REQUIRE_MIGRATION_BACKUP", "LAS_MIGRATION_BACKUP_RECEIPT",
    "LAS_MIGRATION_BACKUP_MAX_AGE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_primary_wins_over_alias(monkeypatch):
    monkeypatch.setenv("LAS_LLM_API_KEY", "new-key")
    monkeypatch.setenv("CLIPROXY_API_KEY", "old-key")
    assert cfg.llm_api_key() == "new-key"


def test_alias_fallback(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "old-key")
    assert cfg.llm_api_key() == "old-key"


def test_defaults():
    assert cfg.llm_base_url() == "http://127.0.0.1:8317/v1"
    assert cfg.llm_model() == "deepseek-ai/DeepSeek-V4-Flash"
    assert cfg.nats_url() == "nats://127.0.0.1:4222"
    assert cfg.gateway_url() == ""
    assert cfg.gateway_jwt_file() is None
    assert cfg.llm_api_key() == ""


def test_gateway_jwt_file_takes_precedence_and_is_reread(monkeypatch, tmp_path):
    token_file = tmp_path / "gateway.jwt"
    token_file.write_text("first.jwt.token\n", encoding="utf-8")
    monkeypatch.setenv("LAS_GATEWAY_JWT_FILE", str(token_file))
    monkeypatch.setenv("LAS_GATEWAY_API_KEY", "ignored-api-key")
    assert cfg.gateway_bearer_token() == "first.jwt.token"
    token_file.write_text("rotated.jwt.token\n", encoding="utf-8")
    assert cfg.gateway_bearer_token() == "rotated.jwt.token"


@pytest.mark.parametrize("content", ["", "two tokens", "line1\nline2"])
def test_gateway_jwt_file_rejects_invalid_content(monkeypatch, tmp_path, content):
    token_file = tmp_path / "gateway.jwt"
    token_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LAS_GATEWAY_JWT_FILE", str(token_file))
    with pytest.raises(ValueError, match="LAS_GATEWAY_JWT_FILE"):
        cfg.gateway_bearer_token()


def test_state_db_derived_from_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    assert cfg.state_db() == tmp_path / "runtime" / "agent-state.db"


def test_state_db_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("LAS_STATE_DB", str(tmp_path / "x.db"))
    assert cfg.state_db() == tmp_path / "x.db"


def test_kimi_api_key_never_read(monkeypatch):
    # Kimi Work 桌面端注入的 KIMI_API_KEY 不得被当作 LLM 密钥
    monkeypatch.setenv("KIMI_API_KEY", "should-not-be-used")
    assert cfg.llm_api_key() == ""


def test_webui_security_config(monkeypatch):
    monkeypatch.setenv(
        "LAS_WEBUI_TOKENS", '{"admin-token-0123456789":"admin"}')
    monkeypatch.setenv("LAS_WEBUI_SESSION_TTL", "3600")
    monkeypatch.setenv("LAS_WEBUI_COOKIE_SECURE", "true")
    monkeypatch.setenv("LAS_WEBUI_REQUIRE_AUTH", "yes")
    assert cfg.webui_tokens() == {"admin-token-0123456789": "admin"}
    assert cfg.webui_session_ttl() == 3600
    assert cfg.webui_cookie_secure() is True
    assert cfg.webui_require_auth() is True


def test_orchestrator_require_auth(monkeypatch):
    assert cfg.orchestrator_require_auth() is False
    monkeypatch.setenv("LAS_ORCH_REQUIRE_AUTH", "1")
    assert cfg.orchestrator_require_auth() is True


def test_migration_backup_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LAS_WORKSPACE", str(tmp_path))
    assert cfg.require_migration_backup() is False
    assert cfg.migration_backup_receipt() == (
        tmp_path / "runtime" / "migration-backup-receipt.json")
    assert cfg.migration_backup_max_age() == 86400
    monkeypatch.setenv("LAS_REQUIRE_MIGRATION_BACKUP", "true")
    monkeypatch.setenv("LAS_MIGRATION_BACKUP_MAX_AGE", "600")
    assert cfg.require_migration_backup() is True
    assert cfg.migration_backup_max_age() == 600


@pytest.mark.parametrize("value", [
    "not-json", "[]", '{}', '{"short":"admin"}',
    '{"admin-token-0123456789":"owner"}',
])
def test_webui_tokens_fail_closed(monkeypatch, value):
    monkeypatch.setenv("LAS_WEBUI_TOKENS", value)
    with pytest.raises(ValueError):
        cfg.webui_tokens()


@pytest.mark.parametrize("value", ["299", "604801"])
def test_webui_session_ttl_bounds(monkeypatch, value):
    monkeypatch.setenv("LAS_WEBUI_SESSION_TTL", value)
    with pytest.raises(ValueError):
        cfg.webui_session_ttl()
