# voice-code — agent orientation

Voice and phone front-end over **real, interactive Claude Code sessions**: every model interaction is a CC session living as a window in the `voice` tmux session on the Mac; the iPhone is a thin PWA over Tailscale; any terminal can `tmux attach` and drive the same sessions. `PROJECT_BRIEF.md` (v5) is the canonical design — read it before any work here.

## State

- v5 built 2026-07-03, replacing the v4 pass the same week (context in the brief). All tests green, ruff clean, cleanup ledger executed.
- NOT yet done: any real deploy (Tailscale still not installed on this Mac), device testing on an iPhone, live Cartesia traffic (no account/key exists yet). The brief's gated real-session test (`tests/test_live_convo.py`, skipped unless `VOICECODE_LIVE=1`) was run once on 2026-07-03 and **passed** — it spawns a real haiku CC session in tmux session `voice-qa`, so the exact production convo spec (fable/low) has not itself been live-driven through the test.
- GitHub remote not created; intended home `github.com/ryan-scheinberg/voice-code`, GPLv3. Everything stays local until Ryan says otherwise.

## Commands

- `uv sync` — install (Python 3.13 via uv; 3.14 is too new for the audio ecosystem)
- `uv run pytest` — full suite (pytest-asyncio auto mode: plain `async def` tests work)
- `uvx ruff check .` — lint; keep it clean
- `uv run python -m voicecode.server` — the service (needs tmux + provider keys)
- `uv run python -m voicecode.ambient` — Mac mic+speaker mode, no phone (same needs)

## Layout

```
voicecode/
  substrate.py     LOAD-BEARING  the ONLY tmux-aware module: spawn/inject/kill CC sessions
  transcript.py    LOAD-BEARING  the ONLY module that parses CC transcript JSONL
  convo.py                       ConvoBridge: voice/UI face of the voice:convo session
  bootstrap.py                   ensure_convo: reuse alive window / --resume dead / fresh spawn
  protocol.py      LOAD-BEARING  WebSocket messages client⇄server + audio formats
  config.py                      host/port/data_dir; VOICECODE_* env overrides
  keychain.py                    secrets via macOS Keychain, env vars win in dev/tests
  adapters/                      stt.py + tts.py ABCs; deepgram_stt.py, cartesia_tts.py
  audio/                         state.py machine, pipeline.py (barge-in, latency), chunker.py
                                 (sentence chunking for TTS), roundtrip.py (live TTS→STT check)
  server/                        app.py (DI factory), ws.py, runtime.py, workstreams.py,
                                 approvals.py, api.py, auth.py (LOAD-BEARING scrypt contract),
                                 store.py, logs.py; __main__.py = composition root
  web/                           static PWA (vanilla JS, self-contained, no build step)
  ambient.py                     local Mac mic+speaker composition root
hooks/ask_phone.py               phone-approval relay hook client (stdin hook JSON → POST → verdict)
deploy/                          launchd plist template (__UV__ __REPO__ __HOME__ __PORT__)
skills/                          role-convo, role-stint-plan, role-inject, deploy (+ scripts/)
plugins/claude-code/             plugin wrapper: commands/deploy.md + skills symlink
tests/                           pytest; mirror module names; server_fakes.py = reusable DI fakes
```

"LOAD-BEARING" = frozen contract; don't change without checking every consumer (and Ryan for protocol-visible shapes). `hash_secret` in `server/auth.py` is shared with the deploy scripts; a golden-vector test breaks if the scrypt parameters move.

## Conventions

- Tests mock at the tmux/transcript boundary: `FakeTmux`/`FakeSubstrate` + fixture JSONL transcript files. **No default test ever launches a real Claude Code session or touches a real tmux server** — the one exception, `tests/test_live_convo.py`, is skipped unless `VOICECODE_LIVE=1` and must only be run deliberately. Provider SDKs are mocked at the SDK boundary; read the installed package source in `.venv` for the real API surface — versions drift.
- Composition roots (`voicecode/server/__main__.py`, `voicecode/ambient.py`) are the only modules that import concrete Tmux/adapters; everything else takes dependencies as constructor args so tests inject fakes.
- Secrets only via `keychain.get_secret("deepgram-api-key" | "cartesia-api-key")` — env vars (`DEEPGRAM_API_KEY`, …) win, which is also how tests inject fakes. Pairing secrets stored as scrypt hashes: `pairing-token-hash`, `pin-hash`. No model API key exists anywhere.

## Decisions you don't get to reopen without Ryan

- **Every model interaction is a real interactive Claude Code session** — no Agent SDK, no headless/print mode, no raw model API calls. EVER.
- Session roster: convo = Fable 5 low (persistent); stint planner and injector = Opus 4.8 high (ephemeral); workstreams = Fable 5 xhigh (until shipped).
- The tmux session is named `voice`; all CC sessions start at `~` so every transcript lands in one `~/.claude/projects/<home-slug>/` directory.
- Phone client is a **PWA served via `tailscale serve`** — no native app.
- Python 3.13 via uv, FastAPI. STT Deepgram streaming, TTS Cartesia behind adapters.

## How the assembled system behaves (facts the next agent needs)

**Spawning** — `Substrate.spawn` types `command claude --session-id <minted-uuid> --model … --effort … -n <name> [--settings f] [--plugin-dir d] [initial-prompt]` into a fresh window (`command` defeats shell aliases/functions). Minting the UUID makes the transcript path known before the session exists. Continuity: `bootstrap.ensure_convo` reuses an alive `voice:convo` window, respawns a dead one with `--resume <stored-id>`, or fresh-spawns with `/voice-code:role-convo`. Multiline inject = `load-buffer` + `paste-buffer -p`, 0.5s pause, `Enter`. Slash commands are TYPED (`send-keys -l`), never pasted, so the TUI's command mode triggers.

**ConvoBridge** — `run()` polls the transcript tail (0.25s) and fans every entry to subscribers. `turn(text)` sends the text, then streams TTS-ready sentence chunks from complete `AssistantText` blocks until the transcript's `system/turn_duration` line (`TurnEnd`) arrives; one turn active at a time — a new turn detaches the superseded stream. Input sent mid-turn QUEUES in the session (Milestone-0), so a superseded/barged turn's remaining blocks and TurnEnd land first — the new turn's stream skips exactly that many unfinished turns' entries (they still reach chat via subscribers) and speaks only its own reply. `/compact` appends to the SAME transcript file; tailing continues unbroken.

**Chat sourcing rule** — the CC transcript is the source of truth: assistant text, tool activity, and final user text all render from transcript entries via the fan-out. The audio pipeline's sink carries ONLY user STT interims (`final=False`), state, TTS audio, and speech_end. Typed `text_input` is not locally echoed — it comes back as a transcript user line.

**Approvals** — workstream sessions get a per-boot PreToolUse hook (settings file written by `__main__.py` at boot with a fresh relay token): `hooks/ask_phone.py --gate-bash` relays only Bash commands matching its short destructive list; everything else exits silently and instantly. Relay path: hook JSON on stdin → POST `/approvals` (X-Voicecode-Token) → `ApprovalRequest` card on the phone → WS `Approval` message resolves it → hook prints an allow/deny permission decision. Timeout (60s server / 90s client), non-200, or unreachable service → the hook prints nothing and Claude Code's native permission behavior takes over — that's built-in hook semantics, not our fallback code. Any user hook (Ryan's bash gate) can exec the same client.

**Workstreams** — `plan_stint`: marker ← convo transcript line count, spawn planner with `/voice-code:role-stint-plan convo=… since_line=… output=…`, poll for the output file (2s interval, 300s budget), kill the session, push `StintPlan`. `launch_workstream`: plan file's first line is `Stint: <title>` (hard contract with role-stint-plan) → window `ws-<slug>`, spawned with `/role-root` + the workstream settings file, full plan text pasted as the first message. `send_to_workstream`: injector distills convo-since-marker into a directive file, directive pasted into the workstream, marker advances. `check_in`: a directive through `pipeline.text()` so the convo session itself reads the workstream transcript tail and answers out loud. Cards push every 5s while any workstream exists (status running/gone from window aliveness, 6-line tail).

**Server** — one live socket globally: a new connection takes over; the old socket gets Error + close. Attach replays chat history from the transcript, then live entries stream. Store (SQLite, WAL) holds only: device credentials, the single convo CC session id, workstream rows (name, cc_session_id, window, title, plan_path, status), and the plan/inject since-marker — conversation content is never stored, and boot drops the legacy `sessions`/`transcript` tables. `create_app(..., web_dir=)` lets tests serve a temp PWA dir. `/healthz` unchanged.

**Audio** — pipeline states `listening/thinking/speaking/interrupted`; barge-in (any non-empty STT chunk while SPEAKING) kills TTS instantly and abandons the sentence stream, but the session keeps writing — the full reply still lands in chat from the transcript. User speech endpointing while THINKING supersedes the in-flight turn. Latency instrumentation: logger `voicecode.latency`, one JSON line per turn, key metric `endpoint_to_first_audio_ms` — measure, don't promise.

## Gotchas already known

- Ryan's zsh defines a `claude()` function that rewrites a bare `claude` invocation — the substrate must always spawn `command claude` with explicit args (it does; keep it that way).
- Transcript JSONL is an undocumented internal format, pinned at CC 2.1.201 in `transcript.py` (one line per content block; meta/sidechain lines skipped; tool results come back as `user` lines). Expect breakage on Claude Code updates — smoke-test parsing after any CC update.
- Text blocks appear in the transcript only when complete — no streaming partials, so first audio waits on the first finished block. role-convo's short spoken turns keep this small; measure before mitigating.
- The default tests never talk to tmux or Claude Code; only `VOICECODE_LIVE=1 uv run pytest tests/test_live_convo.py` (never run automatically) proves the live path.
- Deepgram v7: `client.listen.v1.connect` is the nova-3 socket; `/v2` is Flux-only (no interim_results/endpointing); `utterance_end_ms` minimum is 1000; idle sockets drop ~10s → keepalive every 5s (e.g. while muted).
- Cartesia 3.3.0: SSE chunk events need the `.audio` property (base64-decoded); `AsyncCartesia` does NOT read the key from env — pass it explicitly; `DEFAULT_VOICE_ID` is a stock-voice placeholder until Ryan picks one on the real account.
- `qrcode.make()` returns an image with no `print_ascii`; build a `qrcode.QRCode()` object instead (the deploy skill's pairing step depends on this).
- `audioop` is gone in Python 3.13 — `roundtrip.py` resamples with pure-python linear interpolation.
- PWA runtime QA recipe: headless WebKit via `uv run --with playwright==1.60` (matching browsers already cached in `~/Library/Caches/ms-playwright`, no download). The PWA is fully self-contained — no CDN/external resources; the tailnet has no internet guarantee.
- launchd's default PATH misses Homebrew; the plist template sets PATH so the service can find `tmux`. The service bootstraps the `voice` tmux session itself at boot — deploy only verifies tmux exists.
