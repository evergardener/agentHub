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


def api_token() -> str:
    """orchestrator A2A 端点的调用方鉴权 token（X-Agent-Token 头）。

    面向外部总控（如用户自建的 hermes）。LAS_API_TOKEN 优先，
    回退 LAS_ADAPTER_TOKEN（单租户可共用）；均空 = 关闭（仅开发）。
    """
    return _env("LAS_API_TOKEN") or adapter_token()


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
