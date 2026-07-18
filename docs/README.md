# Remote Workstreams documentation

Remote Workstreams is a Mac-hosted voice and chat control plane for real Claude Code or Codex CLI sessions. The iPhone is a thin PWA client on the same Tailscale network; the Mac owns the agent sessions, audio pipeline, transcripts, approvals, and state.

The local-first voice option uses [Moonshine Voice](https://github.com/moonshine-ai/moonshine) for streaming speech-to-text and local Kokoro/Piper voices for text-to-speech. Deepgram and Cartesia remain supported as optional cloud adapters. Local voice does not replace the coding agent: Claude Code and Codex still run through their installed CLIs and their existing authentication.

## Read next

| Need | Guide |
| --- | --- |
| Install and try local voice | [Quickstart](quickstart.md) |
| Understand every environment variable | [Configuration](configuration.md) |
| Deploy launchd, Tailscale, and iPhone pairing | [Deployment](deployment.md) |
| Understand the runtime boundaries | [Architecture](architecture.md) |
| Know where data and secrets live | [Storage](storage.md) and [Security](security.md) |
| Understand the SQLite records | [Data model](data-model.md) |
| Work on the code | [Development](development.md) and [Contributing](contributing.md) |
| Operate or repair a running install | [Operations](operations.md) and [Troubleshooting](troubleshooting.md) |

## Supported shape

- macOS host with `uv`, `tmux`, and at least one logged-in agent CLI.
- iPhone Safari PWA over Tailscale; no public internet endpoint is required.
- One active WebSocket client at a time; reconnecting takes over the live runtime.
- Voice input is 16 kHz mono signed PCM; voice output is 24 kHz mono signed PCM.
- SQLite stores control metadata. Agent transcript JSONL remains the source of truth for chat and workstream history.
