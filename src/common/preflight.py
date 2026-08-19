"""Fail-closed production configuration checks without printing secrets."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path


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


def check_production_env(path: Path) -> list[Finding]:
    if not path.is_file():
        return [Finding("error", ".env", "文件不存在；先复制 .env.example")]
    findings: list[Finding] = []
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
    require_secret("LAS_GATEWAY_API_KEY", 24)
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
    require_secret("LAS_WEBUI_SESSION_SECRET", 32)

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
                   or meta.get("worker") not in {"codex", "kimi", "dsh"}
                   or not meta.get("peer") for token, meta in peers.items()):
                findings.append(Finding(
                    "error", "LAS_A2A_PEERS",
                    "token 至少 24 字符，且每项必须含合法 peer/worker"))
        except (json.JSONDecodeError, ValueError):
            findings.append(Finding(
                "error", "LAS_A2A_PEERS", "不是合法的非空 peer 映射 JSON"))

    if not _enabled(env.get("LAS_WEBUI_COOKIE_SECURE", "false")):
        findings.append(Finding(
            "warning", "LAS_WEBUI_COOKIE_SECURE",
            "当前仅适用于 loopback HTTP；经 HTTPS 反代时必须设为 true"))
    return findings


def render(findings: list[Finding]) -> str:
    if not findings:
        return "PASS: production configuration checks passed"
    return "\n".join(
        f"{item.level.upper()}: {item.key}: {item.message}"
        for item in findings)


def exit_code(findings: list[Finding], strict: bool = False) -> int:
    return int(any(item.level == "error" or strict for item in findings))
