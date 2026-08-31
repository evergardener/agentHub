#!/usr/bin/env bash
# bootstrap.sh — Phase 0 环境准备（设计文档 §20 Phase 0）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${AGENT_WORKSPACE:-$HOME/AgentWorkspace}"

echo "==> 创建 AgentWorkspace 目录结构 ($WORKSPACE)"
mkdir -p "$WORKSPACE"/{config,projects,tasks,runtime/pids,logs,scripts}

echo "==> 创建 Python venv"
cd "$REPO_ROOT"
python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

echo "==> 初始化 PostgreSQL"
python -c "from state.db import init_db; c = init_db(); c.close(); print('db ok')"

echo "==> 运行测试"
pytest -q

echo "==> bootstrap 完成"
