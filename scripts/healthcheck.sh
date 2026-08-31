#!/usr/bin/env bash
# healthcheck.sh — Phase 0 验收：nats healthy / PostgreSQL available / workspace initialized
set -euo pipefail

WORKSPACE="${AGENT_WORKSPACE:-$HOME/AgentWorkspace}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ok=0; fail=0

check() {  # check <name> <command...>
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $name"; ok=$((ok+1))
  else
    echo "FAIL  $name"; fail=$((fail+1))
  fi
}

check "workspace initialized"  test -d "$WORKSPACE/runtime"
check "postgresql available"   env PYTHONPATH="$REPO_ROOT/src" \
  "$REPO_ROOT/.venv/bin/python" -m common.healthcheck database
check "nats server healthy"    curl -sf "http://127.0.0.1:8222/healthz"
check "jetstream enabled"      curl -sf "http://127.0.0.1:8222/jsz"

echo "---"
echo "pass=$ok fail=$fail"
[ "$fail" -eq 0 ]
