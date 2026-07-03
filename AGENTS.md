# voice-code — agent orientation

Voice interface for Claude Code: Mac runs everything (FastAPI service, dual-layer conversation engine, audio pipeline), iPhone is a thin PWA client over Tailscale. `PROJECT_BRIEF.md` is the canonical design — read it before any work here.

## State

- Brief written 2026-07-02; all v1 decisions locked (see brief).
- First implementation pass merged 2026-07-02: foundation contracts plus all six units (engine, execution adapter, audio pipeline, server, PWA, deploy plugin) built in parallel and assembled. 218 tests green (incl. real-engine+real-pipeline integration tests adopted from QA), ruff clean, bridge evals 6/6 mocked.
- NOT yet done: any live-API run (bridge evals `--live`, roundtrip, real Deepgram/Cartesia traffic), a real deploy (Tailscale still not installed), device testing on an iPhone. Cartesia account doesn't exist yet.
- GitHub remote not created yet; intended home is `github.com/ryan-scheinberg/voice-code`, GPLv3. Everything stays local until Ryan says otherwise.

## Commands

- `uv sync` — install (Python 3.13 via uv; 3.14 is too new for the audio ecosystem)
- `uv run pytest` — full test suite (pytest-asyncio in auto mode: plain `async def` tests work)
- `uvx ruff check .` — lint; keep it clean
- `uv run python -m evals.bridge` — bridge coherence evals against a mock model; `--live` is double-gated (flag + `VOICECODE_LIVE_EVALS=1` + Anthropic key)
- `uv run python -m voicecode.server` — the service; `uv run python -m voicecode.ambient` — Mac mic+speaker mode (both need live provider keys)

## Layout

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
    claude_code.py         Claude Agent SDK adapter (+ claude_code_distill.py)
    deepgram_stt.py        Deepgram Nova-3 streaming (v7 websocket API)
    cartesia_tts.py        Cartesia streaming (SSE path)
  engine/                  conversation agent: conversation.py, prompt.py, chunker.py, dispatch.py
  audio/
    state.py       FROZEN  pipeline state machine (protocol-visible states)
    pipeline.py            AudioPipeline + AudioSink (surface frozen) + latency timings
    roundtrip.py           headless TTS→STT connection test (needs live keys; exit-code result)
  server/                  app.py (DI factory), ws.py, sessions.py, store.py, auth.py, api.py
  web/                     static PWA (vanilla JS, self-contained, no build step)
  ambient.py               local Mac mic+speaker composition root
deploy/                    launchd plist template (__UV__ __REPO__ __HOME__ placeholders)
skills/deploy/             the /voice-code:deploy skill + idempotent scripts/
plugins/claude-code/       plugin wrapper: commands/deploy.md + skills symlink
.claude-plugin/            marketplace manifest
evals/                     bridge coherence evals
tests/                     pytest; mirror module names; server_fakes.py = reusable DI fakes
```

"FROZEN" = load-bearing contract; don't change without checking every consumer (and Ryan for protocol-visible shapes).

## Conventions

- Tests never hit live provider APIs — mock at the SDK boundary. The SDKs are installed in `.venv`; read the installed package source for the real API surface instead of trusting memory. Versions drift.
- Composition roots (`voicecode/server/__main__.py`, `voicecode/ambient.py`) are the only places that import concrete adapter/engine classes; everything else takes dependencies as constructor args so tests inject fakes.
- Secrets only via `keychain.get_secret("anthropic-api-key" | "deepgram-api-key" | "cartesia-api-key")` — env vars `ANTHROPIC_API_KEY` etc. win, which is also how tests inject fakes. Pairing secrets stored as hashes: `pairing-token-hash`, `pin-hash`.

## Decisions you don't get to reopen without Ryan

- Conversation layer is a **raw Anthropic API streaming loop** (Haiku, no tools, owned message list) — NOT a Claude Code session. The execution layer is the Claude Code session (Agent SDK) behind `ExecutionAdapter`.
- Phone client is a **PWA served via `tailscale serve`** — no native app, no React Native in v1.
- Python 3.12/3.13 via uv (not 3.14), FastAPI.
- TTS behind an adapter, Cartesia default. STT is Deepgram streaming.
- Codex: interface only in v1.
- Dispatch (decided 2026-07-02, orchestrator call — flag to Ryan if evals show Haiku missing it): the conversation agent requests execution work by embedding `<dispatch>concise directive</dispatch>` at the end of its raw reply — a prompt convention, not a tool. The engine strips it from spoken chunks and exposes it via `take_dispatch()`; the composition root routes it to the ExecutionAdapter (start() on first dispatch, send() thereafter).

## How the assembled system behaves (facts the next agent needs)

**Engine** — `proactive_turn()` makes no API call unless a `completed`/`needs_approval` event is queued; progress/finding events ride along in the next real turn. The raw reply (dispatch tag included) stays in the owned message list — turn-to-turn memory of handoffs. Event reminders carry `summary` only; `detail` never reaches the model. Frozen system prompt lives in `engine/prompt.py` (single static block with the `cache_control` breakpoint).

**Execution adapter** — `ClaudeCodeAdapter(config, client_factory=None)`: `client_factory(options)` is the test seam (see `FakeClient` in `tests/test_claude_code_adapter.py`). `events()` is single-consumer, sentinel-terminated; the adapter is restartable (start→stop→start). `TaskStarted` is emitted for every `start()` AND `send()`. SDK facts (0.2.110): `can_use_tool` forces streaming mode (connect empty, then `client.query()` per turn); session id arrives via the `system/init` message; permission requests dispatch concurrently (N gates can't deadlock); gates fire only for calls the user's existing Claude Code permission rules evaluate to "ask".

**Audio** — Deepgram v7: `client.listen.v1.connect` is the nova-3 socket; `/v2` is Flux-only (no interim_results/endpointing); `utterance_end_ms` minimum is 1000. Cartesia 3.3.0: SSE chunk events need the `.audio` property (base64-decoded); `AsyncCartesia` does NOT read the key from env — pass it explicitly; `DEFAULT_VOICE_ID` is a stock-voice placeholder until Ryan picks one. Latency: logger `voicecode.latency`, one JSON line per turn, key metric `endpoint_to_first_audio_ms`. Barge-in contract: the engine owns its message list; the pipeline closes `turn()`'s generator on interrupt and accepts whatever the engine recorded (mid-stream: nothing — not even a partial reply). Dispatch follows history: a mid-stream interrupt has no dispatch to route, but once the reply fully streams it is committed to history tag-and-all, so its dispatch routes even if playback is barged into afterward. `tests/test_integration_engine_pipeline.py` and `tests/test_integration_server_assembly.py` (adopted from first-pass QA) pin this seam with the real engine + real pipeline.

**Server** — `SessionRuntime` stays alive in memory across disconnects (execution pump keeps running; events buffer and flush on re-attach); SQLite is the restart-resume path. Both paths are tested. One live socket globally — a new connection takes over and the old socket gets an Error + close. `Sessions` is pushed right after every `Ready` (initial and post-switch). `hash_secret` in `server/auth.py` is the frozen scrypt contract shared with the deploy scripts; a golden-vector test breaks if the parameters move. `create_app(..., web_dir=)` lets tests serve a temp PWA dir.

**PWA** — fully self-contained (no CDN/external resources; tailnet has no internet guarantee). Mute drops mic frames client-side AND sends `Mute`. Typed `text_input` is not locally echoed — the server echoes it as a user `Transcript` via `pipeline.text()`. Runtime QA recipe: headless WebKit via `uv run --with playwright==1.60` (matching browsers already cached in `~/Library/Caches/ms-playwright`, no download).

**Deploy plugin** — `scripts/check.sh` (read-only state report), `store_secret.sh` (Keychain; `--hash` routes through the frozen `hash_secret`), `install_service.sh` (sync → render plist → bootstrap → healthz wait). All idempotent; a deploy re-run is a repair run. `plugins/claude-code/commands/deploy.md` makes `/voice-code:deploy` a literal slash command.

## Gotchas already known

- Prompt caching on the conversation agent is a latency requirement — frozen system prompt, `cache_control` breakpoint, all dynamic context injected after it. **BUT: Haiku's cache minimum is 4096 tokens and the frozen prompt is ~800**, so the system-block breakpoint alone won't cache until context grows. Watch `cache_creation_input_tokens` in the live eval run; the fix, if needed, is a second breakpoint on conversation history.
- Mid-conversation `role:"system"` messages are Opus 4.8-only; on Haiku, inject execution status events as `<system-reminder>` blocks in the user turn.
- Latency target is ≤1.0s p50 (user stops speaking → first audio), instrumented per turn. The spec-v3 claim of 200–300ms was rejected as unrealistic.
- Barge-in shapes the audio pipeline state machine — build it in from the start, don't retrofit.
- Tailscale is NOT installed on this Mac yet; the deploy plugin's install-guidance path is real, not hypothetical.
- No Cartesia account/API key exists yet; nothing can hit the real TTS API until Ryan creates one.
- `qrcode.make()` returns an image with no `print_ascii`; build a `qrcode.QRCode()` object instead.
- `audioop` is gone in Python 3.13 — `roundtrip.py` resamples with pure-python linear interpolation.
- Spoken replies are capped at `_MAX_TOKENS = 1024` in the engine — sized for voice turns; revisit if replies truncate.
