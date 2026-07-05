# voice-code

Hold a natural spoken conversation with Claude Code while it does real agentic work.
Your Mac runs everything — the audio pipeline, the sessions, the state. Your iPhone is
a thin browser client reached over your own tailnet. No cloud infrastructure beyond
the STT and TTS APIs; nothing between your phone and your Mac but Tailscale.

The core design: **every model interaction is a real, interactive Claude Code
session**, living as a window in one tmux session on your Mac. The phone and the
laptop drive the *same* sessions — walk over, `tmux attach`, keep typing. Every
session inherits your full Claude Code setup (skills, hooks, CLAUDE.md, permission
rules) natively, and no model API key exists anywhere — all model use rides Claude
Code auth. A persistent conversation session talks with you; planner and injector
sessions turn that conversation into **workstreams** — execution sessions you watch
as live cards on the phone. If the phone drops — call, dead spot, Safari suspending —
the sessions live on in tmux; reconnect and resume mid-conversation.

## Topology

```
iPhone (Safari PWA) ──WebSocket/HTTPS over Tailscale──> Mac
                                                         ├─ FastAPI service (launchd, persistent)
                                                         │   ├─ Audio pipeline: Deepgram STT ⇄ VAD ⇄ Cartesia TTS
                                                         │   ├─ tmux session "voice": convo + workstream
                                                         │   │    Claude Code sessions (attach from any terminal)
                                                         │   ├─ Transcript tailing: Claude Code JSONL is the chat
                                                         │   ├─ Store: SQLite (credentials, session ids, markers)
                                                         │   └─ Static PWA
                                                         └─ tailscale serve (TLS on the MagicDNS name)
```

## Requirements

- A Mac that stays on (the service runs under launchd), with
  [uv](https://docs.astral.sh/uv/) and [tmux](https://github.com/tmux/tmux)
- A [Tailscale](https://tailscale.com) account, with the Mac and iPhone on the same tailnet
- API keys: [Deepgram](https://console.deepgram.com) (streaming STT),
  [Cartesia](https://play.cartesia.ai) (streaming TTS)
- Claude Code on the Mac, logged in (every session is your existing Claude Code setup —
  skills, hooks, MCP servers and all)

## Install

From Claude Code:

```
/plugin marketplace add ryan-scheinberg/voice-code
/plugin install voice-code@voice-code
/voice-code:deploy
```

`/voice-code:deploy` is a guided deploy run by Claude on your Mac. It confirms every
system-touching action with you before running it, and it is safe to re-run — it doubles
as repair. What it does:

1. Preflight: macOS, uv, tmux, and a durable git clone of this repo (defaults to `~/voice-code`)
2. Tailscale: detects it, guides install and login if missing, captures your MagicDNS name
3. Stores your two provider keys (Deepgram, Cartesia) in the macOS Keychain
4. Takes your 4-digit pairing PIN; only its scrypt hash is stored
5. Installs and starts the launchd service, verifies `/healthz`
6. Maps HTTPS on your MagicDNS name to the local service via `tailscale serve`
7. Prints a pairing QR code — open it on the iPhone, Add to Home Screen, enter the
   PIN, confirm Face ID
8. Runs an audio round-trip test (synthesized speech in → transcript → reply audio out)
   and reports the result

## Security model

- **Tailnet is the perimeter.** The service binds to localhost; only `tailscale serve`
  exposes it, and only devices on your tailnet can reach it at all.
- **Pairing, once per device:** your 4-digit PIN + WebAuthn registration (Face ID);
  the passkey's public key is stored. Five wrong PINs lock pairing for 10 minutes.
- **Login, every app open:** one Face ID tap (WebAuthn assertion against the stored
  passkey) mints a session token held only in server memory (24h TTL) and only in a
  page variable on the phone — a Lock button, a reload, or a server restart ends it.
- **Secrets live in the macOS Keychain** (service `voice-code`) — provider keys, and
  only a *hash* (scrypt) of the PIN. Nothing secret in config files.
- **Passkeys are listed and revocable server-side.** Lose a phone, revoke its
  credential.

## Latency

Measured, not promised: every turn logs endpoint → transcript → first-sentence →
first-audio timestamps. Replies come from a real Claude Code session, so expect the
pace of a thoughtful colleague, not a kiosk — sentence-chunked streaming TTS starts
speaking as soon as a reply lands, and barge-in (speak over the assistant and it
stops) keeps you in control.

## Development

```
uv sync            # install (Python 3.12/3.13)
uv run pytest      # test suite (no live API calls; SDK boundaries are mocked)
uvx ruff check .   # lint
```

| Path | What it is |
|---|---|
| `voicecode/substrate.py` | tmux substrate — spawn/inject/kill Claude Code sessions as windows |
| `voicecode/transcript.py` | Claude Code transcript JSONL parsing (the only format-aware module) |
| `voicecode/convo.py` | ConvoBridge — the voice/UI face of the persistent conversation session |
| `voicecode/protocol.py` | WebSocket messages client ⇄ server, audio formats |
| `voicecode/config.py` | Runtime config; `VOICECODE_*` env overrides |
| `voicecode/keychain.py` | Secrets via the macOS Keychain; env vars win in dev/tests |
| `voicecode/adapters/` | `STTAdapter`, `TTSAdapter` + Deepgram, Cartesia implementations |
| `voicecode/audio/` | Pipeline state machine (`listening/thinking/speaking/interrupted`), round-trip test |
| `voicecode/server/` | FastAPI service, WebSocket, workstreams, approvals, SQLite store, auth |
| `voicecode/web/` | The static PWA |
| `hooks/` | `ask_phone.py` — the phone-approval relay hook client |
| `skills/` | `role-convo`, `role-stint-plan`, `role-inject`, and the deploy skill |
| `plugins/claude-code/` | Claude Code plugin wrapper (`/voice-code:deploy` + skills) |
| `tests/` | pytest, mirroring module names |

## Tailscale Funnel

`tailscale funnel` can expose the service to the public internet. This is documented as
a pointer only and is **unsupported**: voice-code's auth assumes the tailnet perimeter,
and v1 has no public-internet hardening (rate limiting, lockout). Don't do it unless you
understand exactly what you're removing.

## License

[GPLv3](LICENSE).
