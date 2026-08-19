"""统一环境变量配置 — Evolution v3 M2：env-only，不使用 macOS Keychain。

命名规则：LAS_* 为正式名；旧名保留为别名（过渡期内两边都认）。
所有密钥只从环境变量读取，不入库、不入仓、不落 Keychain。

变量一览（正式名 → 旧别名）：
  LAS_WORKSPACE         → AGENT_WORKSPACE      任务工作区根目录
  LAS_STATE_DB          → AGENT_STATE_DB       SQLite 状态库路径
  LAS_NATS_URL          → NATS_URL             NATS 地址
  LAS_DATABASE_URL      （无别名）              postgresql://… 或 sqlite:///…
  LAS_GATEWAY_URL       → AGENT_GATEWAY_URL    agentgateway 地址（空=直连）
  LAS_GATEWAY_API_KEY   → GATEWAY_API_KEY      gateway Bearer key
  LAS_LLM_BASE_URL      → KIMI_API_BASE        OpenAI 兼容端点
  LAS_LLM_API_KEY       → CLIPROXY_API_KEY     端点密钥
  LAS_LLM_MODEL         → KIMI_MODEL           模型名
  LAS_HINDSIGHT_URL     → HINDSIGHT_API_URL    Hindsight 记忆服务
  LAS_HINDSIGHT_API_KEY → HINDSIGHT_API_KEY    Hindsight 密钥
  LAS_HEARTBEAT_INTERVAL → AGENT_HEARTBEAT_INTERVAL  心跳间隔秒
  LAS_LEASE_TTL         → AGENT_LEASE_TTL      agent 租约秒
  LAS_A2A_PEERS         （无别名）              orchestrator A2A peer 映射（JSON）
  LAS_ACTION_RECEIPT_SECRET（无别名）           ActionIntent receipt HMAC 密钥
  LAS_WEBUI_TOKENS      （无别名）              WebUI token→role JSON 映射
  LAS_WEBUI_SESSION_SECRET（无别名）            WebUI session cookie HMAC 密钥
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_WORKSPACE = Path.home() / "AgentWorkspace"
DEFAULT_LLM_BASE = "http://127.0.0.1:8317/v1"
DEFAULT_LLM_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_HINDSIGHT_URL = "http://127.0.0.1:18888"


def _env(primary: str, *aliases: str, default: str = "") -> str:
    """正式名优先，其次旧别名，最后默认值。"""
    for name in (primary, *aliases):
        val = os.environ.get(name)
        if val:
            return val
    return default


def workspace() -> Path:
    return Path(_env("LAS_WORKSPACE", "AGENT_WORKSPACE",
                     default=str(DEFAULT_WORKSPACE)))


def state_db() -> Path:
    explicit = _env("LAS_STATE_DB", "AGENT_STATE_DB")
    if explicit:
        return Path(explicit)
    return workspace() / "runtime" / "agent-state.db"


def database_url() -> str:
    """统一数据库入口（v3 §4）：LAS_DATABASE_URL 优先；
    未配置时由 workspace/state_db 派生 sqlite:/// URL。"""
    url = _env("LAS_DATABASE_URL")
    if url:
        return url
    return f"sqlite:///{state_db()}"


def nats_url() -> str:
    return _env("LAS_NATS_URL", "NATS_URL", default=DEFAULT_NATS_URL)


def gateway_url() -> str:
    """空串表示直连 adapter，不经 gateway。"""
    return _env("LAS_GATEWAY_URL", "AGENT_GATEWAY_URL").strip()


def gateway_api_key() -> str:
    return _env("LAS_GATEWAY_API_KEY", "GATEWAY_API_KEY")


def adapter_token() -> str:
    """Worker adapter 的调用方鉴权 token（X-Agent-Token 头）。

    空串 = 不启用鉴权（本地开发默认值）；生产/常驻部署必须配置。
    直连与经 gateway 两种路径都会携带该头（gateway 默认透传）。
    """
    return _env("LAS_ADAPTER_TOKEN")


def action_receipt_secret() -> str:
    """ActionIntent receipt HMAC secret; adapter token is migration fallback."""
    return _env("LAS_ACTION_RECEIPT_SECRET") or adapter_token()


def api_token() -> str:
    """orchestrator A2A 端点的调用方鉴权 token（X-Agent-Token 头）。

    面向外部总控（如用户自建的 hermes）。LAS_API_TOKEN 优先，
    回退 LAS_ADAPTER_TOKEN（单租户可共用）；均空 = 关闭（仅开发）。
    """
    return _env("LAS_API_TOKEN") or adapter_token()


def webui_tokens() -> dict[str, str]:
    """WebUI login token → role mapping (viewer/operator/admin).

    Empty means authentication is disabled and is only valid for loopback
    development. Malformed security configuration always fails closed.
    """
    import json

    raw = _env("LAS_WEBUI_TOKENS").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LAS_WEBUI_TOKENS 不是合法 JSON: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ValueError("LAS_WEBUI_TOKENS 必须是非空 {token: role} 字典")
    allowed = {"viewer", "operator", "admin"}
    tokens: dict[str, str] = {}
    for token, role in data.items():
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("LAS_WEBUI_TOKENS 中每个 token 至少 16 个字符")
        if role not in allowed:
            raise ValueError(
                "LAS_WEBUI_TOKENS role 仅允许 viewer/operator/admin")
        tokens[token] = role
    return tokens


def webui_session_secret() -> str:
    return _env("LAS_WEBUI_SESSION_SECRET")


def webui_session_ttl() -> int:
    ttl = int(_env("LAS_WEBUI_SESSION_TTL", default="28800"))
    if not 300 <= ttl <= 604800:
        raise ValueError("LAS_WEBUI_SESSION_TTL 必须在 300..604800 秒之间")
    return ttl


def webui_cookie_secure() -> bool:
    return _env("LAS_WEBUI_COOKIE_SECURE", default="false").lower() in {
        "1", "true", "yes", "on"}


def webui_require_auth() -> bool:
    return _env("LAS_WEBUI_REQUIRE_AUTH", default="false").lower() in {
        "1", "true", "yes", "on"}


# peer→worker 固定映射允许的 worker 白名单（Hermes 接入约定，
# 新增 worker 需在此显式放行，防止外部总控 fan-out 到未约定的执行体）。
ALLOWED_PEER_WORKERS = frozenset({"codex", "kimi", "dsh"})


def a2a_peers() -> dict[str, dict[str, str]]:
    """orchestrator A2A v1.0 Bearer peer 映射：token → {peer, worker}。

    LAS_A2A_PEERS 为单行 JSON：
      {"<token>": {"peer": "qishuo-codex", "worker": "codex"}, ...}

    每个 token 代表一个外部总控（hermes）注册的逻辑 peer，服务端依据
    认证 identity 固定路由到指定 worker——不信任请求体自称的
    metadata.agent。worker 仅允许 ALLOWED_PEER_WORKERS 内的值。
    空 = 未配置任何 peer（此时仅 legacy X-Agent-Token 可用）。
    配置畸形直接抛 ValueError：安全相关配置必须启动即失败，不静默降级。
    """
    import json

    raw = _env("LAS_A2A_PEERS").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LAS_A2A_PEERS 不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("LAS_A2A_PEERS 必须是 {token: {peer, worker}} 字典")
    peers: dict[str, dict[str, str]] = {}
    for token, meta in data.items():
        if not token or not isinstance(meta, dict):
            raise ValueError("LAS_A2A_PEERS 每项必须是 token: {peer, worker}")
        peer, worker = str(meta.get("peer", "")).strip(), str(
            meta.get("worker", "")).strip()
        if not peer or worker not in ALLOWED_PEER_WORKERS:
            raise ValueError(
                f"LAS_A2A_PEERS 项 {peer or token[:6] + '…'} 非法："
                f"peer 必填，worker 仅允许 {sorted(ALLOWED_PEER_WORKERS)}")
        peers[token] = {"peer": peer, "worker": worker}
    return peers


def llm_base_url() -> str:
    return _env("LAS_LLM_BASE_URL", "KIMI_API_BASE",
                default=DEFAULT_LLM_BASE).rstrip("/")


def llm_api_key() -> str:
    # 刻意不读 KIMI_API_KEY：Kimi Work 桌面端会注入同名变量指向其自有
    # 网关，会造成 401。
    return _env("LAS_LLM_API_KEY", "CLIPROXY_API_KEY")


def llm_model() -> str:
    return _env("LAS_LLM_MODEL", "KIMI_MODEL", default=DEFAULT_LLM_MODEL)


def hindsight_url() -> str:
    return _env("LAS_HINDSIGHT_URL", "HINDSIGHT_API_URL",
                default=DEFAULT_HINDSIGHT_URL)


def hindsight_api_key() -> str:
    return _env("LAS_HINDSIGHT_API_KEY", "HINDSIGHT_API_KEY")


def heartbeat_interval() -> float:
    return float(_env("LAS_HEARTBEAT_INTERVAL", "AGENT_HEARTBEAT_INTERVAL",
                      default="30"))


def lease_ttl() -> int:
    return int(_env("LAS_LEASE_TTL", "AGENT_LEASE_TTL", default="90"))
