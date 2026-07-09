#!/usr/bin/env bash
# Installs (or reinstalls) the remote-workstreams launchd service and waits for /healthz.
# Idempotent: re-running syncs deps, rewrites the plist, and restarts the service.
#
# Usage: install_service.sh REPO_DIR
set -euo pipefail

REPO="$(cd "${1:?usage: install_service.sh REPO_DIR}" && pwd)"
LABEL="com.remote-workstreams.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$REPO/deploy/$LABEL.plist.template"
PORT="${REMOTE_WORKSTREAMS_PORT:-8400}"
CHATGPT_CODEX="/Applications/ChatGPT.app/Contents/Resources/codex"
if [ -n "${REMOTE_WORKSTREAMS_CODEX_COMMAND:-}" ]; then
  CODEX_COMMAND="$REMOTE_WORKSTREAMS_CODEX_COMMAND"
elif [ -x "$CHATGPT_CODEX" ]; then
  CODEX_COMMAND="$CHATGPT_CODEX"
else
  CODEX_COMMAND="codex"
fi

command -v uv >/dev/null 2>&1 || { echo "error=uv-missing hint=https://docs.astral.sh/uv/"; exit 1; }
UV="$(command -v uv)"
# The service bootstraps the "voice" tmux session at start; without tmux it can't boot.
command -v tmux >/dev/null 2>&1 || { echo "error=tmux-missing hint=brew install tmux"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "error=template-missing path=$TEMPLATE"; exit 1; }

(cd "$REPO" && uv sync)
echo "deps=synced"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.remote-workstreams/logs"  # launchd won't create log dirs
sed -e "s|__UV__|$UV|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__PORT__|$PORT|g" -e "s|__CODEX_COMMAND__|$CODEX_COMMAND|g" "$TEMPLATE" > "$PLIST"
echo "plist=$PLIST"
echo "codex_command=$CODEX_COMMAND"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "service=loaded"

for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "healthz=ok"
    exit 0
  fi
  sleep 1
done
echo "healthz=failed hint=read the StandardOutPath/StandardErrorPath log files named in $PLIST"
exit 1
