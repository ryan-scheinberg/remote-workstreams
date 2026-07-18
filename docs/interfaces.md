# Interfaces

## HTTP

- `GET /healthz` → `{"ok": true}` for local and launchd checks.
- `POST /api/pair/start`, `POST /api/pair/finish` → WebAuthn registration.
- `POST /api/login/start`, `POST /api/login/finish` → WebAuthn authentication and a session token.
- `GET /api/credentials`, `POST /api/credentials/{credential_id}/revoke` → authenticated credential management.

## WebSocket

The PWA connects to `/ws`, sends a JSON `hello` first, then exchanges typed control messages and binary 16 kHz mono PCM frames. Server frames cover readiness, state, user interim transcript, chat transcript, audio, workstream cards/logs, approvals, and `{\"type\":\"error\",\"message\":...}` failures. See `remote_workstreams/protocol.py` for the frozen wire schema.

## Adapter contracts

`STTAdapter.stream(audio)` consumes async PCM chunks and yields `TranscriptChunk`. `TTSAdapter.synthesize(text)` yields signed PCM chunks at `TTS_FORMAT`; `cancel()` must be idempotent and stop future audio promptly. Provider-specific SDK objects stay behind those interfaces.
