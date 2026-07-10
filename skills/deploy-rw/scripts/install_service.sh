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
CODEX_COMMAND="$(command -v codex || printf '%s' codex)"
STT_PROVIDER="${REMOTE_WORKSTREAMS_STT_PROVIDER:-deepgram}"
TTS_PROVIDER="${REMOTE_WORKSTREAMS_TTS_PROVIDER:-cartesia}"
MOONSHINE_LANGUAGE="${REMOTE_WORKSTREAMS_MOONSHINE_LANGUAGE:-en}"
MOONSHINE_STT_MODEL="${REMOTE_WORKSTREAMS_MOONSHINE_STT_MODEL:-medium-streaming}"
MOONSHINE_TTS_LOCALE="${REMOTE_WORKSTREAMS_MOONSHINE_TTS_LOCALE:-en-us}"
MOONSHINE_TTS_VOICE="${REMOTE_WORKSTREAMS_MOONSHINE_TTS_VOICE:-kokoro_af_heart}"
MOONSHINE_TTS_SPEED="${REMOTE_WORKSTREAMS_MOONSHINE_TTS_SPEED:-1.0}"
MOONSHINE_MODEL_DIR="${REMOTE_WORKSTREAMS_MOONSHINE_MODEL_DIR:-$HOME/.remote-workstreams/models/moonshine}"

command -v uv >/dev/null 2>&1 || { echo "error=uv-missing hint=https://docs.astral.sh/uv/"; exit 1; }
UV="$(command -v uv)"
# The service bootstraps the "voice" tmux session at start; without tmux it can't boot.
command -v tmux >/dev/null 2>&1 || { echo "error=tmux-missing hint=brew install tmux"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "error=template-missing path=$TEMPLATE"; exit 1; }

(cd "$REPO" && if [ "$STT_PROVIDER" = moonshine ] || [ "$TTS_PROVIDER" = moonshine ]; then uv sync --extra local-voice; else uv sync; fi)
echo "deps=synced"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.remote-workstreams/logs"  # launchd won't create log dirs
sed -e "s|__UV__|$UV|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__PORT__|$PORT|g" -e "s|__CODEX_COMMAND__|$CODEX_COMMAND|g" -e "s|__STT_PROVIDER__|$STT_PROVIDER|g" -e "s|__TTS_PROVIDER__|$TTS_PROVIDER|g" -e "s|__MOONSHINE_LANGUAGE__|$MOONSHINE_LANGUAGE|g" -e "s|__MOONSHINE_STT_MODEL__|$MOONSHINE_STT_MODEL|g" -e "s|__MOONSHINE_TTS_LOCALE__|$MOONSHINE_TTS_LOCALE|g" -e "s|__MOONSHINE_TTS_VOICE__|$MOONSHINE_TTS_VOICE|g" -e "s|__MOONSHINE_TTS_SPEED__|$MOONSHINE_TTS_SPEED|g" -e "s|__MOONSHINE_MODEL_DIR__|$MOONSHINE_MODEL_DIR|g" "$TEMPLATE" > "$PLIST"
echo "plist=$PLIST"
echo "codex_command=$CODEX_COMMAND"
echo "stt_provider=$STT_PROVIDER"
echo "tts_provider=$TTS_PROVIDER"

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
