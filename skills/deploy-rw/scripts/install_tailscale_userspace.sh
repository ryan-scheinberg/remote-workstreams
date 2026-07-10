#!/usr/bin/env bash
# Installs a persistent, unprivileged Tailscale userspace daemon on macOS.
# This is the fallback when the Tailscale app/kernel extension is unavailable.
# Usage: install_tailscale_userspace.sh [HOME_DIR]
set -euo pipefail

HOME_DIR="${1:-$HOME}"
HOME_DIR="$(cd "$HOME_DIR" && pwd)"
LABEL="com.tailscale.userspace"
PLIST="$HOME_DIR/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$(cd "$(dirname "$0")/../../.." && pwd)/deploy/$LABEL.plist.template"
SOCKET="$HOME_DIR/.local/share/tailscale/tailscaled.sock"
STATE="$HOME_DIR/.local/share/tailscale/tailscaled.state"
LOG_DIR="$HOME_DIR/.local/share/tailscale/logs"

TAILSCALED="$(command -v tailscaled || true)"
[ -n "$TAILSCALED" ] || { echo "error=tailscaled-missing hint=brew install tailscale"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "error=template-missing path=$TEMPLATE"; exit 1; }

mkdir -p "$HOME_DIR/Library/LaunchAgents" "$(dirname "$SOCKET")" "$LOG_DIR"
PATH_VALUE="$(dirname "$TAILSCALED"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
sed -e "s|__TAILSCALED__|$TAILSCALED|g" \
    -e "s|__SOCKET__|$SOCKET|g" \
    -e "s|__STATE__|$STATE|g" \
    -e "s|__HOME__|$HOME_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    -e "s|__PATH__|$PATH_VALUE|g" \
    "$TEMPLATE" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
loaded=0
for _ in 1 2 3 4 5; do
  if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    loaded=1
    break
  fi
  sleep 1
done
[ "$loaded" -eq 1 ] || { echo "error=launchd-bootstrap-failed plist=$PLIST"; exit 1; }

echo "daemon=loaded"
echo "socket=$SOCKET"
echo "plist=$PLIST"
