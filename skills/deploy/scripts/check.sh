#!/usr/bin/env bash
# Reports voice-code deploy state as key=value lines. Read-only: changes nothing,
# always exits 0. Run it before deploying (to pick the needed steps) and after
# any step (to verify it took).
#
# Usage: check.sh [REPO_DIR]   (default: ~/voice-code)
set -uo pipefail

REPO="${1:-$HOME/voice-code}"
PORT="${VOICECODE_PORT:-8400}"

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

# --- tmux (hard prerequisite: every Claude Code session lives in tmux) ---
if command -v tmux >/dev/null 2>&1; then
  echo "tmux=$(command -v tmux)"
  echo "tmux_version=$(tmux -V | awk '{print $2}')"
else
  echo "tmux=missing"
fi

# --- service repo (a git clone of voice-code, NOT the plugin marketplace copy) ---
if [ -f "$REPO/pyproject.toml" ] && grep -q '^name = "voice-code"' "$REPO/pyproject.toml"; then
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
  if security find-generic-password -s voice-code -a "$name" >/dev/null 2>&1; then
    echo "secret_${name}=present"
  else
    echo "secret_${name}=missing"
  fi
done

# --- launchd service ---
PLIST="$HOME/Library/LaunchAgents/com.voicecode.server.plist"
if [ -f "$PLIST" ]; then echo "plist=$PLIST"; else echo "plist=missing"; fi
if launchctl print "gui/$(id -u)/com.voicecode.server" >/dev/null 2>&1; then
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
