#!/usr/bin/env bash
# agentctl-host.sh — 宿主机直连模式的 agentctl 包装
#
# hermes/agentctl 不必须跑在容器里：基础设施（NATS/PG/gateway）都已映射到
# 127.0.0.1，宿主机 .venv 直接连即可。本脚本负责把 .env 翻译为宿主机视角：
#   - PG：compose 内部地址 postgres:5432 → 127.0.0.1:5432
#   - gateway：容器域名 agentgateway → 127.0.0.1:8300
#   - LLM：.env 本来就是宿主机视角，无需改动
#
# 用法:  ./scripts/agentctl-host.sh chat
#        ./scripts/agentctl-host.sh task list
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
[ -f .env ] && { set -a; source .env; set +a; }

export LAS_NATS_URL="${LAS_NATS_URL:-nats://127.0.0.1:4222}"
export LAS_DATABASE_URL="${LAS_DATABASE_URL:-postgresql://agenthub:${LAS_PG_PASSWORD:-agenthub-dev-only}@127.0.0.1:5432/agenthub}"
export LAS_GATEWAY_URL="${LAS_GATEWAY_URL:-http://127.0.0.1:8300}"

exec "$ROOT/.venv/bin/agentctl" "$@"
