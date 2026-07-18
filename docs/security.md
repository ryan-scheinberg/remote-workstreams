# Security

- Keep `HOST=127.0.0.1`; Tailscale is the network boundary.
- Store Deepgram, Cartesia, and pairing secrets in the `remote-workstreams` macOS Keychain service. Do not commit, print, or export them.
- Use a unique four-digit PIN and WebAuthn/Face ID for the phone. Revoke lost devices with the authenticated credentials endpoint.
- Treat transcript JSONL and the SQLite store as sensitive local data. They can contain code, commands, and conversation history.
- Keep the repository private if its local docs or test fixtures reveal personal paths. Never upload model caches, `.venv`, Keychain exports, SQLite files, or agent sessions.
- Review destructive-command approval behavior on every protocol or hook change; add a regression test before deployment.
