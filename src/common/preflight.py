"""Fail-closed production configuration checks without printing secrets."""

from __future__ import annotations

import ipaddress
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


DEFAULT_AGENTS_FILE = (
    Path(__file__).resolve().parents[2] / "config" / "agents.yaml"
)


@dataclass(frozen=True)
class Finding:
    level: str
    key: str
    message: str


def _value(raw: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        if char == "#" and quote is None and (
                index == 0 or raw[index - 1].isspace()):
            raw = raw[:index]
            break
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    return raw


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise ValueError(f"{path.name}:{lineno} 缺少 '='")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum():
            raise ValueError(f"{path.name}:{lineno} 环境变量名非法")
        values[key] = _value(raw)
    return values


def _enabled(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def check_agent_catalog(path: Path = DEFAULT_AGENTS_FILE) -> list[Finding]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return [Finding(
            "error", "config/agents.yaml",
            f"文件不存在、不可读或 YAML 非法：{type(exc).__name__}")]
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return [Finding(
            "error", "config/agents.yaml", "必须包含 agents 映射")]
    expected = {"codex": True, "kimi": False, "dsh": True}
    findings: list[Finding] = []
    for agent_id, enabled in expected.items():
        agent = agents.get(agent_id)
        if not isinstance(agent, dict) or agent.get("enabled") is not enabled:
            findings.append(Finding(
                "error", f"config/agents.yaml:{agent_id}.enabled",
                f"当前发布候选要求 {agent_id}.enabled={str(enabled).lower()}"))
    return findings


def check_production_env(
    path: Path, *, agents_path: Path = DEFAULT_AGENTS_FILE
) -> list[Finding]:
    if not path.is_file():
        return [Finding("error", ".env", "文件不存在；先复制 .env.example")]
    findings: list[Finding] = check_agent_catalog(agents_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        findings.append(Finding(
            "error", ".env", "权限过宽；执行 chmod 600 .env"))
    try:
        env = parse_env(path)
    except (OSError, UnicodeError, ValueError) as exc:
        return findings + [Finding("error", ".env", str(exc))]

    def require_secret(key: str, minimum: int) -> str:
        value = env.get(key, "")
        if not value or value in {"sk-...", "changeme", "change-me"}:
            findings.append(Finding("error", key, "未配置"))
        elif len(value) < minimum:
            findings.append(Finding(
                "error", key, f"长度不足；至少 {minimum} 个字符"))
        return value

    require_secret("LAS_LLM_API_KEY", 16)

    gateway_url = env.get("LAS_GATEWAY_URL", "").strip()
    gateway_api_key = env.get("LAS_GATEWAY_API_KEY", "")
    gateway_jwt_file = env.get("LAS_GATEWAY_JWT_FILE", "").strip()
    gateway_tls_keys = (
        "LAS_GATEWAY_CA_FILE",
        "LAS_GATEWAY_CLIENT_CERT_FILE",
        "LAS_GATEWAY_CLIENT_KEY_FILE",
    )

    def require_file(key: str, *, private: bool = False) -> bool:
        value = env.get(key, "").strip()
        if not value:
            findings.append(Finding("error", key, "未配置"))
            return False
        path_value = Path(value)
        try:
            if not path_value.is_file():
                raise OSError
            with path_value.open("rb") as stream:
                if not stream.read(1):
                    findings.append(Finding("error", key, "文件为空"))
                    return False
            if private and stat.S_IMODE(path_value.stat().st_mode) & (
                    stat.S_IRWXG | stat.S_IRWXO):
                findings.append(Finding(
                    "error", key, "私密文件权限过宽；应仅 owner 可访问"))
                return False
        except OSError:
            findings.append(Finding("error", key, "文件不存在或不可读"))
            return False
        return True

    if gateway_jwt_file:
        require_file("LAS_GATEWAY_JWT_FILE", private=True)
        if gateway_api_key:
            findings.append(Finding(
                "warning", "LAS_GATEWAY_API_KEY",
                "JWT 文件已启用；API key 将被忽略，应移除"))
    else:
        require_secret("LAS_GATEWAY_API_KEY", 24)

    if gateway_url:
        parsed_gateway = urlparse(gateway_url)
        if (parsed_gateway.scheme not in {"http", "https"}
                or not parsed_gateway.hostname
                or parsed_gateway.username or parsed_gateway.password):
            findings.append(Finding(
                "error", "LAS_GATEWAY_URL",
                "必须是无内嵌凭据的 http(s) URL"))
        elif not _is_loopback(parsed_gateway.hostname):
            if parsed_gateway.scheme != "https":
                findings.append(Finding(
                    "error", "LAS_GATEWAY_URL", "跨主机 gateway 必须使用 HTTPS"))
            if not gateway_jwt_file:
                findings.append(Finding(
                    "error", "LAS_GATEWAY_JWT_FILE",
                    "跨主机 gateway 必须使用可轮换 strict JWT"))
            require_file("LAS_GATEWAY_CA_FILE")
            require_file("LAS_GATEWAY_CLIENT_CERT_FILE")
            require_file("LAS_GATEWAY_CLIENT_KEY_FILE", private=True)
    elif gateway_jwt_file or any(env.get(key, "").strip()
                                 for key in gateway_tls_keys):
        findings.append(Finding(
            "error", "LAS_GATEWAY_URL", "配置 JWT/TLS 文件时不得为空"))

    pg_password = require_secret("LAS_PG_PASSWORD", 16)
    if pg_password == "agenthub-dev-only":
        findings.append(Finding("error", "LAS_PG_PASSWORD", "仍在使用开发默认值"))
    adapter_token = require_secret("LAS_ADAPTER_TOKEN", 24)
    require_secret("LAS_ACTION_RECEIPT_SECRET", 32)

    if not _enabled(env.get("LAS_WEBUI_REQUIRE_AUTH", "")):
        findings.append(Finding(
            "error", "LAS_WEBUI_REQUIRE_AUTH", "生产必须为 true"))
    if not _enabled(env.get("LAS_ORCH_REQUIRE_AUTH", "")):
        findings.append(Finding(
            "error", "LAS_ORCH_REQUIRE_AUTH", "生产必须为 true"))
    if not _enabled(env.get("LAS_REQUIRE_MIGRATION_BACKUP", "")):
        findings.append(Finding(
            "error", "LAS_REQUIRE_MIGRATION_BACKUP", "生产必须为 true"))
    try:
        max_age = int(env.get("LAS_MIGRATION_BACKUP_MAX_AGE", "86400"))
        if not 300 <= max_age <= 604800:
            raise ValueError
    except ValueError:
        findings.append(Finding(
            "error", "LAS_MIGRATION_BACKUP_MAX_AGE",
            "必须在 300..604800 秒之间"))
    require_secret("LAS_WEBUI_SESSION_SECRET", 32)

    if not _enabled(env.get("LAS_PRODUCTION_MODE", "")):
        findings.append(Finding(
            "error", "LAS_PRODUCTION_MODE", "生产预检要求显式设为 true"))

    raw_web_tokens = env.get("LAS_WEBUI_TOKENS", "")
    try:
        web_tokens = json.loads(raw_web_tokens) if raw_web_tokens else None
        if not isinstance(web_tokens, dict) or not web_tokens:
            raise ValueError
        allowed_roles = {"viewer", "operator", "admin"}
        if any(not isinstance(token, str) or len(token) < 24
               or role not in allowed_roles
               for token, role in web_tokens.items()):
            findings.append(Finding(
                "error", "LAS_WEBUI_TOKENS",
                "每个 token 至少 24 字符且 role 仅允许 viewer/operator/admin"))
    except (json.JSONDecodeError, ValueError):
        findings.append(Finding(
            "error", "LAS_WEBUI_TOKENS", "必须是非空 token→role JSON 字典"))

    api_token = env.get("LAS_API_TOKEN", "") or adapter_token
    raw_peers = env.get("LAS_A2A_PEERS", "")
    if not api_token and not raw_peers:
        findings.append(Finding(
            "error", "LAS_API_TOKEN/LAS_A2A_PEERS", "至少配置一种 A2A 身份"))
    if api_token and len(api_token) < 24:
        findings.append(Finding(
            "error", "LAS_API_TOKEN", "生产 token 至少 24 字符"))
    if raw_peers:
        try:
            peers = json.loads(raw_peers)
            if not isinstance(peers, dict) or not peers:
                raise ValueError
            if any(not isinstance(token, str) or len(token) < 24
                   or not isinstance(meta, dict)
                   or "worker" in meta
                   or not meta.get("peer") for token, meta in peers.items()):
                findings.append(Finding(
                    "error", "LAS_A2A_PEERS",
                    "token 至少 24 字符，每项只标识 peer，不得绑定 worker"))
        except (json.JSONDecodeError, ValueError):
            findings.append(Finding(
                "error", "LAS_A2A_PEERS", "不是合法的非空 peer 映射 JSON"))

    if _enabled(env.get("LAS_DSH_ALLOW_UNVERIFIED_RUNTIME", "")):
        findings.append(Finding(
            "error", "LAS_DSH_ALLOW_UNVERIFIED_RUNTIME",
            "仅限开发；生产不得绕过 DSH 原生权限门禁"))
    dsh_enabled = _enabled(env.get("LAS_DSH_PRODUCTION_ENABLED", ""))
    if not dsh_enabled:
        findings.append(Finding(
            "error", "LAS_DSH_PRODUCTION_ENABLED",
            "当前发布候选包含已验证 DSH Adapter，生产必须显式启用"))
    if env.get("LAS_DSH_PERMISSION_PRESET", "") != "read-only":
        findings.append(Finding(
            "error", "LAS_DSH_PERMISSION_PRESET",
            "DSH 生产会话必须使用原生 read-only preset"))
    if env.get("LAS_DSH_AGENT_PRESET", "") != "standard":
        findings.append(Finding(
            "error", "LAS_DSH_AGENT_PRESET",
            "DSH 生产会话必须使用已审计的 standard preset"))
    if _enabled(env.get("LAS_KIMI_PRODUCTION_ENABLED", "")):
        findings.append(Finding(
            "error", "LAS_KIMI_PRODUCTION_ENABLED",
            "Kimi 当前因真实 ACP 调用配额耗尽而排除生产路由"))

    hermes_gateway_token = env.get("LAS_HERMES_GATEWAY_API_KEY", "")
    if len(hermes_gateway_token) < 24:
        findings.append(Finding(
            "error", "LAS_HERMES_GATEWAY_API_KEY", "Hermes gateway token 至少 24 字符"))
    elif raw_peers:
        try:
            peers = json.loads(raw_peers)
            if hermes_gateway_token not in peers:
                findings.append(Finding(
                    "error", "LAS_HERMES_GATEWAY_API_KEY",
                    "gateway 与 orchestrator 必须共用同一 qishuo identity token"))
        except json.JSONDecodeError:
            pass

    if not _enabled(env.get("LAS_WEBUI_COOKIE_SECURE", "false")):
        findings.append(Finding(
            "warning", "LAS_WEBUI_COOKIE_SECURE",
            "当前仅适用于 loopback HTTP；经 HTTPS 反代时必须设为 true"))

    alert_url = env.get("LAS_ALERT_WEBHOOK_URL", "").strip()
    alert_token = env.get("LAS_ALERT_WEBHOOK_TOKEN", "")
    if not alert_url:
        findings.append(Finding(
            "warning", "LAS_ALERT_WEBHOOK_URL",
            "未配置外部通知；告警仅保留在 WebUI"))
        if alert_token:
            findings.append(Finding(
                "error", "LAS_ALERT_WEBHOOK_TOKEN", "URL 为空时不得单独配置"))
    else:
        parsed = urlparse(alert_url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password):
            findings.append(Finding(
                "error", "LAS_ALERT_WEBHOOK_URL",
                "生产必须使用无内嵌凭据的 HTTPS URL"))
        if alert_token and len(alert_token) < 16:
            findings.append(Finding(
                "error", "LAS_ALERT_WEBHOOK_TOKEN", "至少 16 个字符"))
    return findings


def render(findings: list[Finding]) -> str:
    if not findings:
        return "PASS: production configuration checks passed"
    return "\n".join(
        f"{item.level.upper()}: {item.key}: {item.message}"
        for item in findings)


def exit_code(findings: list[Finding], strict: bool = False) -> int:
    return int(any(item.level == "error" or strict for item in findings))
