# Data model

`remote_workstreams.server.store.Store` owns a small SQLite database at `Config.db_path` (`~/.remote-workstreams/store.sqlite3` by default). The schema is created by the store on first open; there are no external migrations or hosted database dependencies.

Key records are:

- `settings`: conversation, workstream, planner, injector, enabled-engine, and optional role-skill choices.
- `convo`: the single persistent conversation session ID and engine.
- `marker`: the conversation JSONL line number last consumed by the planner/injector flow.
- `workstreams`: session ID, tmux window, title, plan path, created time, status, model, and engine for every live or recoverable card.
- `credentials`: WebAuthn credential IDs and public-key metadata. Private passkeys remain on the phone.

JSONL transcript files are deliberately outside SQLite. They are append-only provider/session artifacts and remain the source of truth for chat, tool activity, and workstream logs; SQLite stores pointers and control metadata so a service restart can rediscover them.

The service drops the legacy `sessions` and `transcript` tables at store startup. Conversation content is never copied into SQLite.
