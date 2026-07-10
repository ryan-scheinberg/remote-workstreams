# Data model

`remote_workstreams.server.store.Store` owns a small SQLite database at `Config.db_path` (`~/.remote-workstreams/store.sqlite3` by default). The schema is created by the store on first open; there are no external migrations or hosted database dependencies.

Key records are:

- `settings`: engine/provider and planner/injector choices.
- `sessions`: the persistent conversation and workstream session identifiers, engine, tmux window, and transcript markers.
- `workstreams`: titles, launch state, engine/model, and the transcript path used for live cards.
- `credentials`: WebAuthn credential IDs and public-key metadata. Private passkeys remain on the phone.

JSONL transcript files are deliberately outside SQLite. They are append-only provider/session artifacts and remain the source of truth for chat, tool activity, and workstream logs; SQLite stores pointers and control metadata so a service restart can rediscover them.
