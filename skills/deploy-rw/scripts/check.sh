#!/usr/bin/env bash
# Reports remote-workstreams deploy state as key=value lines. Read-only: changes nothing,
# always exits 0. Run it before deploying (to pick the needed steps) and after
# any step (to verify it took).
#
# Usage: check.sh [REPO_DIR]   (default: ~/remote-workstreams)
set -uo pipefail

REPO="${1:-$HOME/remote-workstreams}"
PORT="${REMOTE_WORKSTREAMS_PORT:-8400}"
STT_PROVIDER="${REMOTE_WORKSTREAMS_STT_PROVIDER:-deepgram}"
TTS_PROVIDER="${REMOTE_WORKSTREAMS_TTS_PROVIDER:-cartesia}"
echo "stt_provider=$STT_PROVIDER"
echo "tts_provider=$TTS_PROVIDER"

# --- OS ---
if [ "$(uname -s)" = "Darwin" ]; then
  echo "os=macos"
else
  echo "os=unsupported uname=$(uname -s)"
fi

# --- uv ---
if command -v uv >/dev/null 2>&1; then
  echo "uv=$(command -v uv)"
else
  echo "uv=missing"
fi

# --- tmux (hard prerequisite: every agent session lives in tmux) ---
if command -v tmux >/dev/null 2>&1; then
  echo "tmux=$(command -v tmux)"
  echo "tmux_version=$(tmux -V | awk '{print $2}')"
else
  echo "tmux=missing"
fi

# --- engines: Claude Code is primary; Codex is an optional second engine ---
if command -v claude >/dev/null 2>&1; then
  echo "claude=$(command -v claude)"
else
  echo "claude=missing"
fi
if command -v codex >/dev/null 2>&1; then
  CODEX_COMMAND="$(command -v codex)"
else
  CODEX_COMMAND=""
fi
if [ -n "$CODEX_COMMAND" ]; then
  echo "codex=$CODEX_COMMAND"
  linked=0
  for s in role-convo role-stint-plan role-inject; do
    [ -L "$HOME/.codex/skills/$s" ] && linked=$((linked + 1))
  done
  case "$linked" in
    3) echo "codex_role_skills=linked" ;;
    0) echo "codex_role_skills=missing" ;;
    *) echo "codex_role_skills=partial" ;;
  esac
else
  echo "codex=missing"
fi

# --- role-root: the workstream-launch skill, not part of this repo (deploy-rw offers a fallback) ---
if [ -L "$HOME/.claude/skills/role-root" ] || [ -L "$HOME/.codex/skills/role-root" ]; then
  echo "role_root=present"
else
  echo "role_root=missing"
fi

# --- service repo (a git clone of remote-workstreams, NOT the plugin marketplace copy) ---
if [ -f "$REPO/pyproject.toml" ] && grep -q '^name = "remote-workstreams"' "$REPO/pyproject.toml"; then
  echo "repo=$REPO"
else
  echo "repo=missing checked=$REPO"
fi

# --- tailscale: CLI on PATH, or the Mac app's bundled CLI ---
TS=""
if command -v tailscale >/dev/null 2>&1; then
  TS="$(command -v tailscale)"
elif [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
  TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
fi
if [ -z "$TS" ]; then
  echo "tailscale=missing"
else
  echo "tailscale=$TS"
  status_json="$("$TS" status --json 2>/dev/null)" || status_json=""
  if [ -n "$status_json" ]; then
    # First DNSName in the pretty-printed JSON belongs to the Self block.
    backend="$(printf '%s\n' "$status_json" | sed -n 's/.*"BackendState": *"\([^"]*\)".*/\1/p' | head -1)"
    dnsname="$(printf '%s\n' "$status_json" | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1)"
    echo "tailscale_state=${backend:-unknown}"
    echo "magicdns=${dnsname%.}"
  else
    echo "tailscale_state=down"
  fi
  if "$TS" serve status 2>/dev/null | grep -q "127.0.0.1:$PORT"; then
    echo "serve=configured"
  else
    echo "serve=not-configured"
  fi
fi

# --- Keychain secrets (presence only; values are never printed) ---
for name in deepgram-api-key cartesia-api-key pin-hash; do
  required=1
  [ "$name" = deepgram-api-key ] && [ "$STT_PROVIDER" = moonshine ] && required=0
  [ "$name" = cartesia-api-key ] && [ "$TTS_PROVIDER" = moonshine ] && required=0
  if security find-generic-password -s remote-workstreams -a "$name" >/dev/null 2>&1; then
    echo "secret_${name}=present"
  elif [ "$required" -eq 1 ]; then
    echo "secret_${name}=missing"
  else
    echo "secret_${name}=not-required"
  fi
done
if [ "$STT_PROVIDER" = moonshine ] || [ "$TTS_PROVIDER" = moonshine ]; then
  if [ -x "$REPO/.venv/bin/python" ] && "$REPO/.venv/bin/python" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("moonshine_voice") else 1)' 2>/dev/null; then
    echo "moonshine=installed"
  else
    echo "moonshine=missing"
  fi
  echo "moonshine_model_dir=${REMOTE_WORKSTREAMS_MOONSHINE_MODEL_DIR:-$HOME/.remote-workstreams/models/moonshine}"
fi

# --- launchd service ---
PLIST="$HOME/Library/LaunchAgents/com.remote-workstreams.server.plist"
if [ -f "$PLIST" ]; then echo "plist=$PLIST"; else echo "plist=missing"; fi
if launchctl print "gui/$(id -u)/com.remote-workstreams.server" >/dev/null 2>&1; then
  echo "service=loaded"
else
  echo "service=not-loaded"
fi
if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "healthz=ok"
else
  echo "healthz=unreachable"
fi

# --- tmux session "voice" (the service bootstraps it at start) ---
if command -v tmux >/dev/null 2>&1 && tmux has-session -t voice 2>/dev/null; then
  echo "tmux_session=voice"
  if tmux list-windows -t voice -F '#{window_name}' 2>/dev/null | grep -qx convo; then
    echo "convo_window=alive"
  else
    echo "convo_window=missing"
  fi
else
  echo "tmux_session=missing"
fi

exit 0
