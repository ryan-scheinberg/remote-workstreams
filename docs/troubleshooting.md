# Troubleshooting

| Symptom | Check |
| --- | --- |
| `Moonshine is not installed` | Run `uv sync --extra local-voice` in the service repository. |
| First local run is slow | Model/TTS assets are downloading; wait for the cache under `~/.remote-workstreams/models/moonshine`. |
| No transcript | Confirm the STT provider, model cache, and that the client sends 16 kHz signed PCM. Run the local round trip. |
| No audio | Confirm the TTS provider, voice asset cache, and that the client accepts 24 kHz signed PCM. Toggle hush off. |
| `healthz` is down | Read launchd stderr, run `check.sh`, and verify the repository path in the plist. |
| Phone cannot connect | Both devices must be on the same tailnet; check `tailscale status`, `tailscale serve status`, and the MagicDNS URL. |
| Pairing is locked | Five wrong PINs cause a ten-minute lockout; wait or restart the service, then use the correct PIN. |
| Codex workstream is missing | Codex rollouts do not resume across service restarts; verify `~/.codex/skills` symlinks and the logged-in CLI. |
