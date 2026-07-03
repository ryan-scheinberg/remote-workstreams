# voice-code — agent orientation

Voice interface for Claude Code: Mac runs everything (FastAPI service, dual-layer conversation engine, audio pipeline), iPhone is a thin PWA client over Tailscale. `PROJECT_BRIEF.md` is the canonical design — read it before any work here.

## State

- Brief written 2026-07-02; all v1 decisions locked (see brief). No code yet.
- Next pipeline step: `/plan-to-slices` on the brief (optionally `/iterate-plan` first).
- GitHub remote not created yet; intended home is `github.com/ryan-scheinberg/voice-code`, GPLv3.

## Decisions you don't get to reopen without Ryan

- Conversation layer is a **raw Anthropic API streaming loop** (Haiku, no tools, owned message list) — NOT a Claude Code session. The execution layer is the Claude Code session (Agent SDK) behind `ExecutionAdapter`.
- Phone client is a **PWA served via `tailscale serve`** — no native app, no React Native in v1.
- Python 3.12/3.13 via uv (not 3.14), FastAPI.
- TTS behind an adapter, Cartesia default. STT is Deepgram streaming.
- Codex: interface only in v1.

## Gotchas already known

- Prompt caching on the conversation agent is a latency requirement — frozen system prompt, `cache_control` breakpoint, all dynamic context injected after it.
- Mid-conversation `role:"system"` messages are Opus 4.8-only; on Haiku, inject execution status events as `<system-reminder>` blocks in the user turn.
- Latency target is ≤1.0s p50 (user stops speaking → first audio), instrumented per turn. The spec-v3 claim of 200–300ms was rejected as unrealistic.
- Barge-in shapes the audio pipeline state machine — build it in from the start, don't retrofit.
- Tailscale is NOT installed on this Mac yet; the deploy plugin's install-guidance path is real, not hypothetical.
