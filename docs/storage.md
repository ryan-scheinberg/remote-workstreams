# Storage and data lifecycle

| Data | Location | Notes |
| --- | --- | --- |
| Control metadata | `~/.remote-workstreams/store.sqlite3` | `convo`, `workstreams`, `marker`, `settings`, and WebAuthn `credentials`; no conversation bodies. |
| Runtime logs | `~/.remote-workstreams/logs/server.out.log` and `server.err.log` by default | Logs contain operational events, latency records, and HTTP paths/statuses, not stored API secrets. |
| Local voice assets | `~/.remote-workstreams/models/moonshine/` | Downloaded Moonshine/Kokoro/Piper model files; safe to delete and redownload. |
| Claude transcripts | Claude Code's normal project/session transcript paths | Tailed directly; they are the chat source of truth. |
| Codex rollouts | `~/.codex/sessions/YYYY/MM/DD/` | Tailed as Codex JSONL rollouts. |
| Web assets | `remote_workstreams/web/` | Bundled PWA, served by FastAPI. |

There is no hosted database, object store, or model API key in this project. Back up the data directory and the CLI session directories if you need to preserve history; do not commit either.
