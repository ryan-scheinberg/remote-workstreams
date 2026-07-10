# Storage and data lifecycle

| Data | Location | Notes |
| --- | --- | --- |
| Control metadata | `~/.remote-workstreams/store.sqlite3` | Settings, sessions, workstreams, credential records, and markers. |
| Runtime logs | `~/.remote-workstreams/*.log` or the paths in the launchd plist | Logs contain operational events, not API secrets. |
| Local voice assets | `~/.remote-workstreams/models/moonshine/` | Downloaded Moonshine/Kokoro/Piper model files; safe to delete and redownload. |
| Claude transcripts | Claude Code's normal project/session transcript paths | Tailed directly; they are the chat source of truth. |
| Codex rollouts | `~/.codex/sessions/YYYY/MM/DD/` | Tailed as Codex JSONL rollouts. |
| Web assets | `remote_workstreams/web/` | Bundled PWA, served by FastAPI. |

There is no hosted database, object store, or model API key in this project. Back up the data directory and the CLI session directories if you need to preserve history; do not commit either.
