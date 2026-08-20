#!/usr/bin/env bash
# Install the local-only DSH Web runtime as a macOS LaunchAgent.
set -euo pipefail

ACTION="${1:-install}"
LABEL="top.evergardenviolet.agenthub.dsh-web"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="$HOME/Library/Logs/agenthub-dsh-web.log"
DOMAIN="gui/$(id -u)"

case "$ACTION" in
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL"
    exit 0
    ;;
  install) ;;
  *) echo "usage: install-dsh-web-autostart.sh [install|uninstall]" >&2; exit 2 ;;
esac

DSH_BIN="$(command -v dsh || true)"
[ -n "$DSH_BIN" ] && [ -x "$DSH_BIN" ] || {
  echo "missing dsh executable in the current PATH" >&2
  exit 1
}
DSH_BIN_DIR="$(dirname "$DSH_BIN")"

mkdir -p "$PLIST_DIR" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${DSH_BIN}</string>
    <string>web</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>3080</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${DSH_BIN_DIR}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "installed: $LABEL"
echo "  dsh:   $DSH_BIN"
echo "  url:   http://127.0.0.1:3080"
echo "  plist: $PLIST"
echo "  log:   $LOG"
