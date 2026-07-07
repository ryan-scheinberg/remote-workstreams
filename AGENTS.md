# remote-workstreams — agent orientation

Voice and phone front-end over **real, interactive Claude Code sessions**: every model interaction is a CC session living as a window in the `voice` tmux session on the Mac; the iPhone is a thin PWA over Tailscale; any terminal can `tmux attach` and drive the same sessions. The README carries the design; this file carries what the next agent working on the code needs.

## State

- v5 built 2026-07-03, replacing the v4 pass the same week. All tests green, ruff clean.
- **Device-tested 2026-07-05** end to end: launchd service, `tailscale serve` (tailnet-only), a paired iPhone, real voice turns over live Deepgram+Cartesia, roundtrip passed. First device testing produced two fixes now in main: media-element TTS sink (iOS ringer switch muted Web Audio) and the server-side EchoGuard (iOS echoCancellation let the agent hear itself).
- The gated real-session test (`tests/test_live_convo.py`, skipped unless `REMOTE_WORKSTREAMS_LIVE=1`) was run once on 2026-07-03 and **passed** (haiku in tmux session `voice-qa`).
- Home: `github.com/ryan-scheinberg/remote-workstreams`, GPLv3.

## Commands

- `uv sync` — install (Python 3.13 via uv; 3.14 is too new for the audio ecosystem)
- `uv run pytest` — full suite (pytest-asyncio auto mode: plain `async def` tests work)
- `uvx ruff check .` — lint; keep it clean
- `uv run python -m remote_workstreams.server` — the service (needs tmux + provider keys)
- `uv run python -m remote_workstreams.ambient` — Mac mic+speaker mode, no phone (same needs)

## Layout

```
remote_workstreams/
  substrate.py     LOAD-BEARING  the ONLY tmux-aware module: spawn/inject/kill CC sessions
  transcript.py    LOAD-BEARING  the ONLY module that parses CC transcript JSONL
  convo.py                       ConvoBridge: voice/UI face of the voice:convo session
  bootstrap.py                   ensure_convo: reuse alive window / --resume dead / fresh spawn
  protocol.py      LOAD-BEARING  WebSocket messages client⇄server + audio formats
  config.py                      host/port/data_dir; REMOTE_WORKSTREAMS_* env overrides
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

"LOAD-BEARING" = frozen contract; don't change without checking every consumer (and the maintainer for protocol-visible shapes). `hash_secret` in `server/auth.py` is shared with the deploy scripts; a golden-vector test breaks if the scrypt parameters move.

## Conventions

- Tests mock at the tmux/transcript boundary: `FakeTmux`/`FakeSubstrate` + fixture JSONL transcript files. **No default test ever launches a real Claude Code session or touches a real tmux server** — the one exception, `tests/test_live_convo.py`, is skipped unless `REMOTE_WORKSTREAMS_LIVE=1` and must only be run deliberately. Provider SDKs are mocked at the SDK boundary; read the installed package source in `.venv` for the real API surface — versions drift.
- Composition roots (`remote_workstreams/server/__main__.py`, `remote_workstreams/ambient.py`) are the only modules that import concrete Tmux/adapters; everything else takes dependencies as constructor args so tests inject fakes.
- Secrets only via `keychain.get_secret("deepgram-api-key" | "cartesia-api-key")` — env vars (`DEEPGRAM_API_KEY`, …) win, which is also how tests inject fakes. The pairing PIN is stored as a scrypt hash: `pin-hash` (a stale `pairing-token-hash` entry may linger in the Keychain from the pre-passkey design — nothing reads it; leave it). No model API key exists anywhere.

## Decisions you don't get to reopen without the maintainer

- **Every model interaction is a real interactive Claude Code session** — no Agent SDK, no headless/print mode, no raw model API calls. EVER.
- Session roster: convo = Fable 5 low (persistent); stint planner and injector = Opus 4.8 high (ephemeral); workstreams = Fable 5 xhigh (until shipped).
- The tmux session is named `voice`; all CC sessions start at `~` so every transcript lands in one `~/.claude/projects/<home-slug>/` directory.
- Phone client is a **PWA served via `tailscale serve`** — no native app.
- Python 3.13 via uv, FastAPI. STT Deepgram streaming, TTS Cartesia behind adapters.

## How the assembled system behaves (facts the next agent needs)

**Spawning** — `Substrate.spawn` types `command claude --session-id <minted-uuid> --model … --effort … -n <name> [--settings f] [--plugin-dir d] [initial-prompt]` into a fresh window (`command` defeats shell aliases/functions). Minting the UUID makes the transcript path known before the session exists. Continuity: `bootstrap.ensure_convo` reuses an alive `voice:convo` window, respawns a dead one with `--resume <stored-id>`, or fresh-spawns with `/remote-workstreams:role-convo`. Multiline inject = `load-buffer` + `paste-buffer -p`, 0.5s pause, `Enter`. Slash commands are TYPED (`send-keys -l`), never pasted, so the TUI's command mode triggers.

**ConvoBridge** — `run()` polls the transcript tail (0.25s) and fans every entry to subscribers. `turn(text)` sends the text, then streams TTS-ready sentence chunks from complete `AssistantText` blocks until the transcript's `system/turn_duration` line (`TurnEnd`) arrives; one turn active at a time — a new turn detaches the superseded stream. Input sent mid-turn QUEUES in the session (Milestone-0), so a superseded/barged turn's remaining blocks and TurnEnd land first — the new turn's stream skips exactly that many unfinished turns' entries (they still reach chat via subscribers) and speaks only its own reply. `/compact` appends to the SAME transcript file; tailing continues unbroken.

**Chat sourcing rule** — the CC transcript is the source of truth: assistant text, tool activity, and final user text all render from transcript entries via the fan-out. The audio pipeline's sink carries ONLY user STT interims (`final=False`), state, TTS audio, and speech_end. Typed `text_input` is not locally echoed — it comes back as a transcript user line.

**Approvals** — workstream sessions get a per-boot PreToolUse hook (settings file written by `__main__.py` at boot with a fresh relay token): `hooks/ask_phone.py --gate-bash` relays only Bash commands matching its short destructive list; everything else exits silently and instantly. Relay path: hook JSON on stdin → POST `/approvals` (X-Workstreams-Token) → `ApprovalRequest` card on the phone → WS `Approval` message resolves it → hook prints an allow/deny permission decision. Timeout (60s server / 90s client), non-200, or unreachable service → the hook prints nothing and Claude Code's native permission behavior takes over — that's built-in hook semantics, not our fallback code. Any user hook (e.g. a personal bash gate) can exec the same client.

**Workstreams** — `new_workstream` (one message; the phone button is arm→confirm): marker ← convo transcript line count, spawn planner with `/remote-workstreams:role-stint-plan convo=… since_line=… output=…`, poll for the output file (2s interval, 300s budget), kill the planner, then launch IMMEDIATELY — the plan is trusted, never shown for review. Plan file's first line is `Stint: <title>` (hard contract with role-stint-plan; the title is all the user ever sees) → window `ws-<slug>`, spawned with `/role-root` + the workstream settings file, full plan text pasted only after `_await_ready` sees the role greeting in the transcript. `send_to_workstream`: injector distills convo-since-marker into a directive file, directive pasted into the workstream, marker advances. `check_in`: a directive through `pipeline.text()` so the convo session itself reads the workstream transcript tail and answers out loud. `compact_workstream`: types `/compact` into the workstream session. `end_workstream`: kills the window and drops the row/card; the CC transcript survives. Cards (a one-card swipe pager) carry name/title/status plus transcript-derived vitals (`transcript.SessionVitals`: state waiting/thinking/error, active subagent count, context %); the name's color encodes state (green waiting / blue waiting with subagents / amber mid-turn / red error-or-gone), each Compact button doubles as the context meter (idle label is the %, arming shows the verb), and the push runs every 5s unconditionally — the message also carries the convo session's context % for the action-bar Compact button. Every phone button arms-then-confirms (tap → accented "label?", tap again → fires). `clear_convo`: `bootstrap.fresh_convo` kills the convo window and spawns a brand-new session (`/remote-workstreams:role-convo`, new id, marker → 0), `bridge.reset()` swaps the transcript tail, the manager's `convo_transcript` is re-pointed, and `ConvoCleared` wipes the phone's chat.

**Auth** — pairing (once per device): 4-digit PIN (`POST /api/pair/start`, checked against the Keychain `pin-hash`; 5 misses lock pairing for 10 minutes, in-memory) → WebAuthn registration (Face ID) → `POST /api/pair/finish` stores the passkey (credential id, public key, sign count) in SQLite and returns a session token. Login (every app open): `POST /api/login/start` (discoverable-passkey assertion options) → Face ID → `POST /api/login/finish` verifies against the stored public key and mints a session token. Session tokens are random, held ONLY in an in-memory dict on `LoginManager` (24h TTL) and in a page variable on the phone — never in localStorage; a server restart or the PWA Lock button (also auto-lock after 120s backgrounded) logs everyone out, and the next open is one Face ID tap. WS `hello{credential}` carries the session token and validates only against live sessions. Upgrading across the auth rework drops the old credentials table — previously paired phones must re-pair (PIN + Face ID) once.

**Server** — one live socket globally: a new connection takes over; the old socket gets Error + close. Attach replays chat history from the transcript, then live entries stream. Store (SQLite, WAL) holds only: device credentials, the single convo CC session id, workstream rows (name, cc_session_id, window, title, plan_path, status), and the plan/inject since-marker — conversation content is never stored, and boot drops the legacy `sessions`/`transcript` tables. `create_app(..., web_dir=)` lets tests serve a temp PWA dir. `/healthz` unchanged.

**Audio** — pipeline states `listening/thinking/speaking/interrupted`; barge-in (any non-empty STT chunk while SPEAKING) kills TTS instantly and abandons the sentence stream, but the session keeps writing — the full reply still lands in chat from the transcript. User speech endpointing while THINKING supersedes the in-flight turn. Latency instrumentation: logger `remote_workstreams.latency`, one JSON line per turn, key metric `endpoint_to_first_audio_ms` — measure, don't promise. Two hard-won constraints (2026-07): (1) EchoGuard's window is TIME-anchored — first-audio send time + shipped bytes + 1.5s — so the client must play audio promptly and nothing may stall the playback element while visible; an attempt to gate its force-resume on AudioContext state (plus flush-on-mic-mute) delayed playback past the window and reopened FULL echo, and was reverted — the force-resume is gated on page visibility only, which also keeps a backgrounded frozen stream from screeching. (2) Background chat while app-switched is an iOS web limit, not a bug: iOS suspends the page's AudioContext even in Safari with live capture; if it ever matters, the paths are a native WKWebView shell with a background-audio session (WebAuthn doesn't work in WebViews, so Face ID would go native) or an aiortc WebRTC transport.

## Gotchas already known

- A user's shell may define a `claude()` function or alias that rewrites a bare `claude` invocation — the substrate must always spawn `command claude` with explicit args (it does; keep it that way).
- Transcript JSONL is an undocumented internal format, pinned at CC 2.1.201 in `transcript.py` (one line per content block; meta/sidechain lines skipped; tool results come back as `user` lines). Expect breakage on Claude Code updates — smoke-test parsing after any CC update. `SessionVitals` leans on more of it: `system/turn_duration` ends a turn, `isApiErrorMessage` flags errors, `message.usage` totals ÷ 200k give context % (`compactMetadata.postTokens` resets it after `/compact`; the `isCompactSummary` user line is NOT a prompt), and subagents count Agent/Task `tool_use` until a real `tool_result` — an "Async agent launched" ack keeps a background agent active until its `<task-notification>` user line, matched by `<tool-use-id>`. A typed `/compact` records THREE ways — a bare `/compact` user line at press, the `<command-name>` form, and the recap — and finishes with `system/compact_boundary`, never a `turn_duration`: parse_line skips user strings starting with "/" and `isCompactSummary` lines (they rendered as the user speaking), emits `CompactEnd` for the boundary (→ phone `Compacted` message stops the compact spinner), and vitals clears thinking on it (the card sat amber forever). Remote-control showing two compacts per press is that double-record, not two compactions.
- Text blocks appear in the transcript only when complete — no streaming partials, so first audio waits on the first finished block. role-convo's short spoken turns keep this small; measure before mitigating.
- Echo has bitten three times; the failure mode mutates. Latest (2026-07-06): the guard's "short transcripts always pass" rule let Deepgram's 1-3-word FIRST interims of an echo barge in, which clipped playback at the reply's opening — so the endpointed final was also short, passed again, and committed as phantom user input on EVERY long reply. Fix: short transcripts are echo iff they're the utterance's verbatim word PREFIX (echo.py). If echo regresses again, get evidence before theorizing: the convo CC transcript shows phantom user turns that quote the previous reply's opening words, timestamped seconds after it.
- The default tests never talk to tmux or Claude Code; only `REMOTE_WORKSTREAMS_LIVE=1 uv run pytest tests/test_live_convo.py` (never run automatically) proves the live path.
- Deepgram v7: `client.listen.v1.connect` is the nova-3 socket; `/v2` is Flux-only (no interim_results/endpointing); `utterance_end_ms` minimum is 1000; idle sockets drop ~10s → keepalive every 5s (e.g. while muted).
- Cartesia 3.3.0: SSE chunk events need the `.audio` property (base64-decoded); `AsyncCartesia` does NOT read the key from env — pass it explicitly; `DEFAULT_VOICE_ID` is Cartesia's stock "Skylar"; override via the `voice_id` constructor arg.
- `qrcode.make()` returns an image with no `print_ascii`; build a `qrcode.QRCode()` object instead (the deploy skill's pairing step depends on this).
- `audioop` is gone in Python 3.13 — `roundtrip.py` resamples with pure-python linear interpolation.
- The PWA has no build hashes, so the server stamps `Cache-Control: no-cache` on every response (middleware in `server/app.py`). Without it iOS serves a stale `app.js` straight through deploys — a client-side fix that "didn't work" on the phone is probably this. After a deploy, swipe-kill and reopen the PWA.
- iOS playback runs through a hidden `<audio>` element (`web/audio.js`) because the ringer switch mutes bare Web Audio; the element auto-resumes on pause events and a watchdog keeps it playing while scheduled audio remains — iOS pauses it on audio-session interruptions.
- PWA runtime QA recipe: headless WebKit via `uv run --with playwright==1.60` (matching browsers already cached in `~/Library/Caches/ms-playwright`, no download). The PWA is fully self-contained — no CDN/external resources; the tailnet has no internet guarantee.
- launchd's default PATH misses Homebrew; the plist template sets PATH so the service can find `tmux`. The service bootstraps the `voice` tmux session itself at boot — deploy only verifies tmux exists.
