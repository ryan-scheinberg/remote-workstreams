# voice-code

Voice interface for Claude Code on personal infrastructure. Mac is the brain, iPhone is a thin terminal, Tailscale is the wire. Open source (GPLv3), published as `github.com/ryan-scheinberg/voice-code`.

## Context

Claude Code is keyboard-bound. This project makes it ambient: hold a natural spoken conversation with your coding assistant while it does real agentic work — from anywhere, on any network, with zero cloud infrastructure beyond the model/STT/TTS APIs you already pay for.

The core innovation is a **dual-layer conversation architecture**: a fast conversational model (Haiku) streams natural spoken responses immediately, while a deep execution agent (a headless Claude Code session) runs tools, subagents, and plans in parallel. The user never waits on backend work. The execution layer feeds findings into the conversation layer's context; the voice speaks only to what it knows and defers naturally on what it doesn't.

All state lives on the Mac in a persistent service. The phone dropping — call, lost signal, closed app, Safari suspending — is a non-event. Reconnect and resume mid-conversation.

## Audience

- Primary: the author (personal daily driver).
- Secondary: self-hosters who clone the repo and run the deploy plugin on their own Mac + tailnet.

## Scope

### The MVP Slice

A user can hold an ambient voice conversation (Mac mic + speaker, no phone yet) that flows naturally while an execution agent completes real Claude Code work in the background, and hear its findings surface in the spoken conversation.

### In Scope

- Dual-layer conversation engine (conversation agent + execution agent + status-event bridge)
- Full audio pipeline on the Mac: streaming STT (Deepgram Nova-3), VAD/endpointing, sentence-chunked streaming TTS (Cartesia behind an adapter), barge-in
- Persistent FastAPI service (launchd) holding all session state; sessions survive disconnects and are resumable
- PWA phone client served by the Mac over `tailscale serve` (HTTPS on the MagicDNS name): mic capture, playback, Workspace Viewer, session switcher
- Auth: tailnet membership + pairing token + 4-digit PIN + WebAuthn (Face ID), long-lived scoped session credential
- Approval gates: execution-agent permission requests surface in the Workspace Viewer as approve/deny, wired to the Agent SDK permission callback
- Deploy plugin (`/voice-code:deploy`): Tailscale check, provider key setup into macOS Keychain, token/PIN generation, launchd install, pairing QR
- Execution-agent adapter interface with the Claude Code implementation

### Out of Scope

- **Codex implementation** — the adapter interface is designed for it; the implementation lands later. Halves v1 integration testing; Codex isn't installed on the target machine.
- **Native iOS app** — the PWA covers v1. Native Swift (background audio, locked-screen listening) is a follow-on only if pocket-listening proves necessary. Safari suspending is architecturally identical to a dropped phone.
- **Local Whisper STT** — fallback story, not a launch feature. Deepgram streaming is the only option that hits the latency budget.
- **Tailscale Funnel mode** — documented as a pointer for self-hosters; no hardening work (rate limiting, lockout) in v1. Private tailnet is the supported mode.
- **Multiple concurrent voice sessions** — server-side session model supports many stored sessions with one attached/active; simultaneous live audio sessions deferred.
- **Cloud relay of any kind** — permanently out.

## Technical Approach

### Topology

```
iPhone (Safari PWA) ──WebSocket/HTTPS over Tailscale──> Mac
                                                         ├─ FastAPI service (launchd, persistent)
                                                         │   ├─ Audio pipeline: Deepgram STT ⇄ VAD ⇄ Cartesia TTS
                                                         │   ├─ Conversation agent: raw Anthropic API, Haiku, streaming
                                                         │   ├─ Execution agent: Claude Agent SDK (headless Claude Code)
                                                         │   ├─ Session store: SQLite
                                                         │   └─ Static PWA + Workspace Viewer
                                                         └─ tailscale serve (TLS on MagicDNS name)
```

### The two layers are different kinds of thing — this is load-bearing

**Conversation agent** = a plain `client.messages.stream()` loop on `claude-haiku-4-5` (config-swappable to Sonnet). No tools. Short frozen system prompt with a `cache_control` breakpoint — prompt caching here is a latency requirement, not an optimization. We own the message list, which is what makes the context bridge possible.

**Execution agent** = a headless Claude Code session via the Claude Agent SDK (Python), behind an adapter interface:

```
ExecutionAdapter: start(prompt) / send(message) / events() -> stream / resume(session_id) / approve(gate_id, verdict)
```

This inherits the whole harness for free (skills, hooks, MCP, subagents, `--resume`). Approval gates map onto the SDK's permission callback: a gated tool call emits a `needs_approval` event to the Workspace Viewer; the verdict flows back into the running session.

**The bridge**: the execution agent's activity is distilled into typed status events — `task_started`, `progress`, `finding`, `needs_approval`, `completed`, `error` — each a short structured summary. The server injects pending events into the conversation agent's context as `<system-reminder>` blocks inside the next user turn (mid-conversation `role:"system"` messages are Opus 4.8-only; user-turn injection is the documented pattern for Haiku), placed after the cache breakpoint so the prefix cache survives.

**Coherence rule**: the conversation agent's system prompt hard-constrains it to speak only to events it has received and to defer naturally on anything in flight ("still working through the auth module") — never to fabricate results. This prompt + the event schema is the highest-risk design surface; it gets prototyped and evaluated before any audio exists.

**Unsolicited speech**: `completed` and `needs_approval` events trigger proactive speech when the user is silent — that's what ambient means. Client has a mute toggle; muted events queue and surface on unmute or in the Viewer.

### Latency budget (user stops speaking → first audio)

| Stage | Budget |
|---|---|
| VAD endpointing (trailing-silence decision) | 300–500ms |
| STT finalization (Deepgram streaming) | 100–200ms |
| Conversation agent TTFT (Haiku, cached prompt) | 300–500ms |
| First sentence → TTS first audio (Cartesia) | 50–150ms |
| **Target total** | **≤ 1.0s p50** |

Non-negotiable techniques: prompt caching on the conversation agent; sentence-chunked streaming TTS (never wait for the full response); barge-in. Speculative LLM starts on interim transcripts are a known lever held in reserve — added only if measured p50 exceeds 1s.

**Barge-in**: client requests `echoCancellation: true` on `getUserMedia`; server kills TTS synthesis and playback the moment VAD detects user speech during playback. The audio pipeline is a state machine (`listening / thinking / speaking / interrupted`) designed around this from day one.

### Phone client: PWA, not an app

`tailscale serve` provisions real Let's Encrypt certificates for the Mac's MagicDNS name, so Safari treats the served app as a secure context — unlocking `getUserMedia`, WebAuthn (Face ID), and WebSockets in the browser. The client is static HTML/JS served by the Mac: AudioWorklet mic capture streaming PCM over WebSocket, audio playback, the Workspace Viewer, and session controls. No Xcode, no App Store; any tailnet device with a browser is a terminal.

### Auth

1. **Tailnet membership** is the perimeter — non-members can't reach the Mac at all.
2. **Pairing** (once per device): token (32+ chars, generated at deploy) + user-chosen 4-digit PIN → WebAuthn registration (Face ID) → server issues a long-lived, revocable session credential.
3. **Reconnects** present the credential; no re-auth. Credentials listed/revocable server-side.
4. Secrets (provider keys, token hash, PIN hash) live in the macOS Keychain via the `security` CLI; nothing secret in config files.

### Stack

- **Python 3.12/3.13 via uv** (3.14 is too new for parts of the audio ecosystem), FastAPI, uvicorn
- Anthropic Python SDK (conversation layer), Claude Agent SDK (execution layer)
- Deepgram Python SDK (streaming STT + endpointing), Cartesia Python SDK behind a `TTSAdapter` (ElevenLabs adapter later)
- SQLite for session/transcript/credential persistence
- Vanilla JS/TS PWA (no framework needed for a thin client), AudioWorklet
- launchd for service persistence; `tailscale serve` for TLS + routing
- Conversation model: `claude-haiku-4-5` default. Execution model: whatever the user's Claude Code is configured with (Opus 4.8 default) — voice-code doesn't override it.

## Testing & Observability

- **Bridge evals before audio**: scripted text conversations against the dual-layer engine asserting the coherence rule (no fabricated results, natural deferral, findings surfaced within N turns). This is the project's most important test suite.
- **Latency instrumentation from day one**: per-turn structured log of endpoint→transcript→TTFT→first-audio timestamps; the ≤1s p50 criterion is measured, not vibes.
- Unit tests: event schema, session store, auth flows (token/PIN/credential), TTS adapter contract.
- Integration: audio round-trip test (synthesized speech in → transcript → reply audio out) runnable headlessly on the Mac; used by the deploy plugin as its final connection test.
- Soak test for milestone 2: 30-minute PWA session on LTE — mic stability, reconnect behavior, credential persistence.
- Structured JSON logs from the service; `/healthz` endpoint; launchd keeps-alive.

## Deployment & Rollout

Single environment: the user's Mac. Deployment is the `/voice-code:deploy` Claude Code plugin — OS + Tailscale checks (guides install/login if missing), provider key prompts → Keychain, token/PIN setup, launchd install, `tailscale serve` config, pairing QR, audio round-trip test. Rollback = launchd unload + previous git tag. Public repo from the start; "launch" is just README + a post when it's a proven daily driver.

## Risks & Open Questions

- **Haiku coherence under the deferral prompt** — can it consistently avoid fabricating and defer gracefully? *Closed by the milestone-0 bridge evals; fallback is Sonnet on the conversation layer (config flip).*
- **VAD endpointing vs. thinking-out-loud speech** — ambient talk has long mid-thought pauses; too-eager endpointing interrupts, too-lazy feels sluggish. *Closed by tuning Deepgram endpointing against real usage; may need a "hold-to-think" affordance.*
- **Safari mic/AudioWorklet stability over long sessions and reconnects** — *closed by the milestone-2 soak test; native app is the escape hatch.*
- **Echo cancellation quality on iPhone speaker at conversational volume** — barge-in may false-trigger on the assistant's own voice. *Closed by real-device testing; mitigations: TTS-playback-aware VAD gating, headphone recommendation.*
- **Agent SDK permission-callback ergonomics for approval gates** — assumed to map cleanly; *closed by a spike in the execution-adapter work.*
- **Cartesia account** — doesn't exist yet; needed before TTS work starts. *Action: Ryan creates account + API key.*
