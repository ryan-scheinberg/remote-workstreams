# Quickstart: local voice

This path keeps voice free and on-device. It does not install or replace Claude Code or Codex; install and log in to whichever CLI you want the service to drive first.

```bash
cd "/Users/anthonyforca/Documents/Remote Voice Workstreams"
uv sync --extra local-voice
export REMOTE_WORKSTREAMS_STT_PROVIDER=moonshine
export REMOTE_WORKSTREAMS_TTS_PROVIDER=moonshine
uv run python -m remote_workstreams.audio.roundtrip
```

The first run downloads the Moonshine STT model and the selected TTS assets into `~/.remote-workstreams/models/moonshine`. On this M5 Max, the medium streaming STT model is roughly 430 MB; the default Kokoro voice adds roughly 110 MB. Downloads are cached and are not repeated.

For a local development server:

```bash
REMOTE_WORKSTREAMS_STT_PROVIDER=moonshine \
REMOTE_WORKSTREAMS_TTS_PROVIDER=moonshine \
uv run python -m remote_workstreams.server
```

Open `http://127.0.0.1:8400/` on the Mac. For the iPhone flow, use [Deployment](deployment.md), which adds launchd, Tailscale Serve, a pairing PIN, and a WebAuthn passkey.

To switch one side back to a cloud adapter, set that provider to `deepgram` or `cartesia` and store only the corresponding key in the macOS Keychain. Never put keys in `.env`, shell history, or the repository.
