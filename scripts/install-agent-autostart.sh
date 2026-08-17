#!/usr/bin/env bash
# install-agent-autostart.sh — worker adapter 的 launchd 自启安装/卸载（macOS）
#
# 用法:
#   install-agent-autostart.sh codex            安装并立即启动（登录后自动拉起）
#   install-agent-autostart.sh codex uninstall  停止并移除
#
# 生成 ~/Library/LaunchAgents/top.evergardenviolet.agenthub.<agent>.plist：
#   RunAtLoad + KeepAlive（崩溃 10s 后重启），日志在 ~/Library/Logs/agenthub-<agent>.log
set -euo pipefail

AGENT="${1:?usage: install-agent-autostart.sh <agent> [uninstall]}"
ACTION="${2:-install}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="top.evergardenviolet.agenthub.${AGENT}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="${PLIST_DIR}/${LABEL}.plist"
LOG="$HOME/Library/Logs/agenthub-${AGENT}.log"
DOMAIN="gui/$(id -u)"

case "$ACTION" in
  uninstall)
    launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL"
    exit 0
    ;;
  install) ;;
  *) echo "unknown action: $ACTION" >&2; exit 2 ;;
esac

[ -x "$ROOT/.venv/bin/python" ] || { echo "missing .venv, run: python3 -m venv .venv && pip install -e ." >&2; exit 1; }

mkdir -p "$PLIST_DIR"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ROOT}/scripts/agent-worker.sh</string>
    <string>${AGENT}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>
</dict>
</plist>
EOF

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "${DOMAIN}/${LABEL}"
echo "installed: $LABEL"
echo "  plist: $PLIST"
echo "  log:   $LOG"
