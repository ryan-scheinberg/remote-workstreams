# Deployment: Mac and iPhone

1. Use a durable service checkout outside `~/Documents` (the current install uses `~/remote-workstreams-service`). macOS privacy controls can stall Python launch agents when their working tree is under Documents. Run `skills/deploy-rw/scripts/check.sh "$HOME/remote-workstreams-service"` and fix missing `uv`, `tmux`, an agent CLI, or Tailscale first.
2. Install the local voice extra: `uv sync --extra local-voice`.
3. Set `REMOTE_WORKSTREAMS_STT_PROVIDER=moonshine` and `REMOTE_WORKSTREAMS_TTS_PROVIDER=moonshine` in the launchd install environment. No Deepgram or Cartesia key is needed for this mode.
4. Choose a four-digit pairing PIN and store its hash with `skills/deploy-rw/scripts/store_secret.sh pin-hash --hash "$PWD"`. Store cloud keys only if a cloud provider is selected.
5. Run `skills/deploy-rw/scripts/install_service.sh "$HOME/remote-workstreams-service"`. It renders the plist, bootstraps launchd, waits for `/healthz`, and prints the selected providers.
6. When Tailscale is `Running`, expose the service with the syntax shown by `tailscale serve --help` (usually `tailscale serve --bg 8400`).
7. Open the MagicDNS HTTPS URL on the iPhone while Tailscale is connected. Add it to the Home Screen, tap Pair, enter the PIN, and approve Face ID to create the WebAuthn passkey.
8. Finish with `uv run python -m remote_workstreams.audio.roundtrip` and a final `check.sh`.

If a command needs a macOS authorization, Keychain write, launchd change, Tailscale login, or Face ID confirmation, stop and let the operator perform that prompt. The scripts are idempotent; re-running them is the repair path.
