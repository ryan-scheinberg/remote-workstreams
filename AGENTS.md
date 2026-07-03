# voice-code — agent orientation

Voice interface for Claude Code: Mac runs everything (FastAPI service, dual-layer conversation engine, audio pipeline), iPhone is a thin PWA client over Tailscale. `PROJECT_BRIEF.md` is the canonical design — read it before any work here.

## State

- Brief written 2026-07-02; all v1 decisions locked (see brief).
- Foundation committed 2026-07-02: frozen contracts, pyproject with all deps, plugin scaffolding, foundation tests. First implementation pass in flight via parallel unit builds.
- GitHub remote not created yet; intended home is `github.com/ryan-scheinberg/voice-code`, GPLv3. Everything stays local until Ryan says otherwise.

## Commands

- `uv sync` — install (Python 3.13 via uv; 3.14 is too new for the audio ecosystem)
- `uv run pytest` — full test suite (pytest-asyncio in auto mode: plain `async def` tests work)
- `uvx ruff check .` — lint; keep it clean

## Layout and ownership

```
voicecode/
  events.py        FROZEN  typed status events (the bridge vocabulary)
  protocol.py      FROZEN  WebSocket messages client⇄server + audio formats
  config.py        FROZEN  runtime config; VOICECODE_* env overrides
  keychain.py      FROZEN  secrets via macOS Keychain, env vars win in dev/tests
  adapters/
    execution.py   FROZEN  ExecutionAdapter ABC
    stt.py         FROZEN  STTAdapter ABC + TranscriptChunk
    tts.py         FROZEN  TTSAdapter ABC
    claude_code.py         Claude Agent SDK implementation      (unit: execution)
    deepgram_stt.py        Deepgram Nova-3 streaming            (unit: audio)
    cartesia_tts.py        Cartesia streaming                   (unit: audio)
  engine/                  conversation agent + bridge           (unit: engine)
  audio/
    state.py       FROZEN  pipeline state machine (protocol-visible states)
    pipeline.py            AudioPipeline + AudioSink; surface frozen, body owned by unit: audio
  server/                  FastAPI, WS, SQLite store, auth       (unit: server)
  web/                     static PWA                            (unit: pwa)
skills/deploy/             the /voice-code:deploy skill          (unit: plugin)
plugins/claude-code/       Claude Code plugin wrapper (symlinks skills/deploy)
.claude-plugin/            marketplace manifest
evals/                     bridge coherence evals                (unit: engine)
tests/                     pytest; mirror module names
```

"FROZEN" = the contract other units code against. Changing one mid-build breaks siblings working in parallel; if a frozen contract is genuinely wrong, stop and return that finding instead of editing it.

## Build conventions

- Units work on their own worktree branch and stay inside their owned paths. Don't edit `pyproject.toml`, `AGENTS.md`, or `README.md` — return needed dependency additions and doc notes to the orchestrator instead.
- Tests never hit live provider APIs (Anthropic, Deepgram, Cartesia) — mock at the SDK boundary. The SDKs are installed in `.venv`; read the installed package source for the real API surface instead of trusting memory. Versions drift.
- Composition roots (`python -m voicecode.server`, `python -m voicecode.ambient`) may import concrete classes; everything else takes dependencies as constructor args so tests inject fakes.
- Secrets only via `keychain.get_secret("anthropic-api-key" | "deepgram-api-key" | "cartesia-api-key")` — env vars `ANTHROPIC_API_KEY` etc. win, which is also how tests inject fakes.

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
- No Cartesia account/API key exists yet; nothing can hit the real TTS API until Ryan creates one.
