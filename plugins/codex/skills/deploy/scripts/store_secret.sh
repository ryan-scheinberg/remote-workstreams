#!/usr/bin/env bash
# Stores one remote-workstreams secret in the login Keychain (service "remote-workstreams").
# Reads the value from stdin. Idempotent: -U updates an existing entry in place.
#
# Usage:
#   printf '%s' "$VALUE" | store_secret.sh NAME              # API keys, stored as-is
#   printf '%s' "$VALUE" | store_secret.sh NAME --hash REPO  # pairing secrets, hash only
#
# --hash pipes the value through remote_workstreams.server.auth.hash_secret (scrypt,
# salt voice-code-v1 — the server's frozen contract) inside REPO's uv
# environment, so only the hash ever reaches the Keychain.
set -euo pipefail

NAME="${1:-}"
case "$NAME" in
  deepgram-api-key|cartesia-api-key) NEEDS_HASH=no ;;
  pin-hash) NEEDS_HASH=yes ;;
  *) echo "error=unknown-secret name='$NAME'"; exit 2 ;;
esac

VALUE="$(cat)"
if [ -z "$VALUE" ]; then
  echo "error=empty-value"
  exit 2
fi

if [ "${2:-}" = "--hash" ]; then
  if [ "$NEEDS_HASH" != yes ]; then
    echo "error=hash-not-allowed name=$NAME"
    exit 2
  fi
  REPO="${3:?usage: store_secret.sh NAME --hash REPO_DIR}"
  VALUE="$(printf '%s' "$VALUE" | (cd "$REPO" && uv run python -c \
    'import sys; from remote_workstreams.server.auth import hash_secret; print(hash_secret(sys.stdin.read().strip()))'))"
elif [ "$NEEDS_HASH" = yes ]; then
  echo "error=hash-required name=$NAME use=--hash"
  exit 2
fi

security add-generic-password -U -s remote-workstreams -a "$NAME" -w "$VALUE"
echo "stored=$NAME"
