# voice-code

Hold a natural spoken conversation with Claude Code while it does real agentic work.
Your Mac runs everything — the audio pipeline, the conversation engine, the coding
agent, the session state. Your iPhone is a thin browser client reached over your own
tailnet. No cloud infrastructure beyond the model, STT, and TTS APIs you already pay
for; nothing between your phone and your Mac but Tailscale.

The core design is a **dual-layer conversation**: a fast conversational model (Haiku)
streams spoken replies immediately, while a deep execution agent — a headless Claude
Code session — runs tools, subagents, and plans in parallel. You never wait on backend
work. The execution layer feeds typed status events into the conversation layer's
context; the voice speaks only to what it actually knows and defers naturally on
anything still in flight. If the phone drops — call, dead spot, Safari suspending — the
session lives on the Mac; reconnect and resume mid-conversation.

## Topology

```
iPhone (Safari PWA) ──WebSocket/HTTPS over Tailscale──> Mac
                                                         ├─ FastAPI service (launchd, persistent)
                                                         │   ├─ Audio pipeline: Deepgram STT ⇄ VAD ⇄ Cartesia TTS
                                                         │   ├─ Conversation agent: Anthropic API, Haiku, streaming
                                                         │   ├─ Execution agent: Claude Agent SDK (headless Claude Code)
                                                         │   ├─ Session store: SQLite
                                                         │   └─ Static PWA + Workspace Viewer
                                                         └─ tailscale serve (TLS on the MagicDNS name)
```

## Requirements

- A Mac that stays on (the service runs under launchd), with [uv](https://docs.astral.sh/uv/)
- A [Tailscale](https://tailscale.com) account, with the Mac and iPhone on the same tailnet
- API keys: [Anthropic](https://console.anthropic.com),
  [Deepgram](https://console.deepgram.com) (streaming STT),
  [Cartesia](https://play.cartesia.ai) (streaming TTS)
- Claude Code on the Mac (the execution agent is your existing Claude Code setup —
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

1. Preflight: macOS, uv, and a durable git clone of this repo (defaults to `~/voice-code`)
2. Tailscale: detects it, guides install and login if missing, captures your MagicDNS name
3. Stores your three provider keys in the macOS Keychain
4. Generates a pairing token (shown to you exactly once) and takes your 4-digit PIN;
   only scrypt hashes are stored
5. Installs and starts the launchd service, verifies `/healthz`
6. Maps HTTPS on your MagicDNS name to the local service via `tailscale serve`
7. Prints a pairing QR code — open it on the iPhone, Add to Home Screen, enter token +
   PIN, confirm Face ID
8. Runs an audio round-trip test (synthesized speech in → transcript → reply audio out)
   and reports the result

## Security model

- **Tailnet is the perimeter.** The service binds to localhost; only `tailscale serve`
  exposes it, and only devices on your tailnet can reach it at all.
- **Pairing, once per device:** pairing token (44 chars, generated at deploy) + your
  4-digit PIN + WebAuthn registration (Face ID). The server then issues a long-lived,
  revocable session credential; reconnects present the credential, no re-auth.
- **Secrets live in the macOS Keychain** (service `voice-code`) — provider keys, and
  only *hashes* (scrypt) of the pairing token and PIN. Nothing secret in config files.
- **Credentials are listed and revocable server-side.** Lose a phone, revoke its
  credential.

## Latency

The target is **≤ 1.0s p50** from the moment you stop speaking to the first audio of
the reply, and it is measured, not asserted: every turn logs
endpoint → transcript → time-to-first-token → first-audio timestamps. The techniques
that make it possible are non-negotiable in the design: prompt caching on the
conversation agent, sentence-chunked streaming TTS (never wait for the full response),
and barge-in (speak over the assistant and it stops).

## Development

```
uv sync            # install (Python 3.12/3.13)
uv run pytest      # test suite (no live API calls; SDK boundaries are mocked)
uvx ruff check .   # lint
```

| Path | What it is |
|---|---|
| `voicecode/events.py` | Typed status events — the bridge vocabulary between the two layers |
| `voicecode/protocol.py` | WebSocket messages client ⇄ server, audio formats |
| `voicecode/config.py` | Runtime config; `VOICECODE_*` env overrides |
| `voicecode/keychain.py` | Secrets via the macOS Keychain; env vars win in dev/tests |
| `voicecode/adapters/` | `ExecutionAdapter`, `STTAdapter`, `TTSAdapter` + Claude Code, Deepgram, Cartesia implementations |
| `voicecode/engine/` | Conversation agent + the execution-event bridge |
| `voicecode/audio/` | Pipeline state machine (`listening/thinking/speaking/interrupted`), round-trip test |
| `voicecode/server/` | FastAPI service, WebSocket, SQLite session store, auth |
| `voicecode/web/` | The static PWA |
| `skills/deploy/` | The deploy skill + scripts |
| `plugins/claude-code/` | Claude Code plugin wrapper (`/voice-code:deploy`) |
| `evals/` | Bridge coherence evals — the project's most important test suite |
| `tests/` | pytest, mirroring module names |

The execution adapter is an interface; the Claude Code implementation ships in v1. A
Codex implementation is designed for but not included.

## Tailscale Funnel

`tailscale funnel` can expose the service to the public internet. This is documented as
a pointer only and is **unsupported**: voice-code's auth assumes the tailnet perimeter,
and v1 has no public-internet hardening (rate limiting, lockout). Don't do it unless you
understand exactly what you're removing.

## License

[GPLv3](LICENSE).
