# Configuration

`remote_workstreams.config.Config` reads `REMOTE_WORKSTREAMS_*` environment variables. Defaults are safe for a local Mac but use the original Deepgram/Cartesia providers for compatibility; set both providers to `moonshine` for a keyless install.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8400` | Local bind address. Keep the service on loopback; Tailscale Serve proxies it. |
| `DATA_DIR` | `~/.remote-workstreams` | SQLite and runtime state directory. |
| `CODEX_COMMAND` | `codex` | Codex executable used by launchd. |
| `STT_PROVIDER` | `deepgram` | `deepgram` or `moonshine`. |
| `TTS_PROVIDER` | `cartesia` | `cartesia` or `moonshine`. |
| `MOONSHINE_LANGUAGE` | `en` | Moonshine language code. |
| `MOONSHINE_STT_MODEL` | `medium-streaming` | `tiny`, `small`, `base`, or `medium-streaming`. |
| `MOONSHINE_TTS_LOCALE` | `en-us` | Local TTS locale. |
| `MOONSHINE_TTS_VOICE` | `kokoro_af_heart` | Downloadable Kokoro/Piper voice name. |
| `MOONSHINE_TTS_SPEED` | `1.0` | Positive playback speed multiplier. |
| `MOONSHINE_MODEL_DIR` | `~/.remote-workstreams/models/moonshine` | Cache root for local STT/TTS assets. |

Provider selection is independent: local STT can use cloud TTS, or vice versa. The round-trip command checks only the keys required by the selected cloud providers.
