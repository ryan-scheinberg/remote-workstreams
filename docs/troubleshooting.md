# Troubleshooting

| Symptom | Check |
| --- | --- |
| `Moonshine is not installed` | Run `uv sync --extra local-voice` in the service repository. |
| First local run is slow | Model/TTS assets are downloading; wait for the cache under `~/.remote-workstreams/models/moonshine`. |
| No transcript | Confirm the STT provider, model cache, and that the client sends 16 kHz signed PCM. Run the local round trip. |
| No audio | Confirm the TTS provider, voice asset cache, and that the client accepts 24 kHz signed PCM. Toggle hush off. |
| `Face ID failed: This is an invalid domain` | Do not pair from `http://127.0.0.1:8400`; WebAuthn rejects the IP origin. Use `http://localhost:8400` for Mac-only testing or the HTTPS MagicDNS URL for the phone. |
| `healthz` is down | Read launchd stderr, run `check.sh`, and verify the repository path in the plist. |
| Phone cannot connect | Both devices must be on the same tailnet; check `tailscale status`, `tailscale serve status`, and the MagicDNS URL. |
| `another connection took over` | Only one live browser socket is supported. Close every other remote-workstreams tab (including Chrome and the in-app browser), then reload the device you want to use. A taken-over tab now stays offline until it is deliberately reloaded. |
| Pairing is locked | Five wrong PINs cause a ten-minute lockout; wait or restart the service, then use the correct PIN. |
| Codex workstream is missing | Codex rollouts do not resume across service restarts; verify `~/.codex/skills` symlinks and the logged-in CLI. |
