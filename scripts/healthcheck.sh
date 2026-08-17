#!/usr/bin/env bash
# healthcheck.sh — Phase 0 验收：nats healthy / sqlite writable / workspace initialized
set -euo pipefail

WORKSPACE="${AGENT_WORKSPACE:-$HOME/AgentWorkspace}"
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
check "sqlite writable"        test -w "$WORKSPACE/runtime/agent-state.db"
check "nats server healthy"    curl -sf "http://127.0.0.1:8222/healthz"
check "jetstream enabled"      curl -sf "http://127.0.0.1:8222/jsz"

echo "---"
echo "pass=$ok fail=$fail"
[ "$fail" -eq 0 ]
