#!/usr/bin/env bash
# agent-worker.sh — 宿主机 worker adapter 统一启动入口（launchd / 手动通用）
#
# 用法:  agent-worker.sh <agent>     如 agent-worker.sh codex
#
# 行为：
#   1. source 项目根 .env（密钥/端点统一从环境变量来，Evolution v3 M2）
#   2. 补齐 PATH（launchd 默认 PATH 不含 ~/.local/bin，codex/kimi CLI 在那）
#   3. 默认双地址绑定 127.0.0.1 + 本机 LAN IP，可被 LAS_ADAPTER_BIND 覆盖
#   4. exec serve_adapter.py（多 socket 绑定 + token 鉴权由应用层完成）
set -euo pipefail

AGENT="${1:?usage: agent-worker.sh <agent>}"

# 项目根 = scripts/ 的上一级
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
[ -f .env ] && { set -a; source .env; set +a; }

# launchd 环境 PATH 极简；codex/kimi 等 CLI 装在用户目录
export PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# 每 agent 默认端口；可用 LAS_ADAPTER_PORT 覆盖
case "$AGENT" in
  codex) DEFAULT_PORT=8201 ;;
  kimi)  DEFAULT_PORT=8202 ;;
  *)     DEFAULT_PORT=8290 ;;
esac
export LAS_ADAPTER_PORT="${LAS_ADAPTER_PORT:-$DEFAULT_PORT}"
export LAS_ADAPTER_BIND="${LAS_ADAPTER_BIND:-127.0.0.1,192.168.7.10}"
export LAS_AGENT_ENDPOINT="${LAS_AGENT_ENDPOINT:-http://host.docker.internal:$LAS_ADAPTER_PORT}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$ROOT/.venv/bin/python" "$ROOT/scripts/serve_adapter.py" "$AGENT"
