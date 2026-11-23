#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/../com.glint.brain.plist"
LABEL="com.glint.brain"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "[Glint] Unloading $LABEL..."
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "[Glint] Uninstalled $LABEL."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Stop any manually running brain-server bound to port 18765 so launchd can take over cleanly.
EXISTING_PIDS="$(lsof -ti :18765 2>/dev/null || true)"
if [[ -n "$EXISTING_PIDS" ]]; then
  echo "[Glint] Stopping existing process(es) on port 18765: $EXISTING_PIDS"
  kill $EXISTING_PIDS 2>/dev/null || true
  sleep 0.5
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
cp "$PLIST_SRC" "$PLIST_DST"
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "[Glint] Installed and started $LABEL ($PLIST_DST)"
echo "[Glint] Logs: /tmp/com.glint.brain.out.log & /tmp/com.glint.brain.err.log"
