# voice-code (v5)

Voice and phone front-end over **real, interactive Claude Code sessions**. Mac is the brain, tmux is the substrate, terminals are glass, iPhone is a thin terminal, Tailscale is the wire. Open source (GPLv3), intended home `github.com/ryan-scheinberg/voice-code`.

## Context

v4 built a dual-layer engine: a raw Haiku API loop for speed, a headless Agent-SDK Claude Code session for work, and a status-event bridge between them. It shipped and verified — and it was the wrong product. A fast shallow model optimizes for "sounds human at a drive-through"; the actual goal is the best results. Conversations with Fable at low effort beat Haiku by too much to trade away, and a headless SDK session is a second-class agent you can't sit down and drive.

v5's rule: **every model interaction is a real interactive Claude Code session.** No Agent SDK, no headless/print mode, no raw Anthropic API calls — ever. The system gains three things structurally: the phone and the laptop drive the *same* sessions (walk over, attach, keep typing); every session inherits the full harness (skills, hooks, CLAUDE.md, permission rules) natively instead of through an adapter; and no Anthropic API key exists anywhere — all model use rides Claude Code auth.

## The substrate: tmux, terminal-agnostic

All sessions live in one tmux server session (`voice`). The Python service talks **only to tmux and the filesystem**:

- **Write**: `tmux send-keys` / `load-buffer`+`paste-buffer` (multiline-safe) into a window.
- **Read**: never scrape the TUI for content. Claude Code writes every session transcript as JSONL under `~/.claude/projects/<cwd-slug>/`; the service tails those files for replies, status, and chat rendering. `capture-pane` exists only for the raw-terminal view.
- **Attach**: any terminal (Warp today, anything tomorrow) runs `tmux attach -t voice`. The system neither knows nor cares which glass is open. When you come back to the laptop, the sessions are just *there*, clean, yours.

Session↔transcript mapping: every session starts at `~` — workstreams included; `/role-root` spawns its subagents into the right repos itself — so all transcripts land in one `~/.claude/projects/<home-slug>/` directory. The service identifies a session's JSONL as the file that appears after its spawn; the milestone-0 spike pins this.

## The session roster

| Session | Model / effort | Lifetime | Role |
|---|---|---|---|
| `voice:convo` | Fable 5, **low** | persistent | The conversation. A dedicated skill (`role-convo`) makes it a voice-register interlocutor: short spoken turns, no markdown in speech, tools welcome when they're the quick path. Not a tool ban — an orientation: clear success criteria, stay efficient, workstream-sized work goes to workstreams. |
| stint planner | Opus 4.8, **high** | ephemeral | Reads the convo transcript since the last stint → writes a summarized stint plan file → session closes. |
| `voice:ws-<name>` | Fable 5, **xhigh** | until shipped | An execution session opened with `/role-root` + the stint plan. Appears in the UI as a **workstream** card and in tmux as a window. |
| injector | Opus 4.8, **high** | ephemeral | Takes the latest convo delta + a target workstream → distills a clean directive → `send-keys` into that workstream → closes. |

The convo session is the product's soul; `role-convo` is the highest-risk design surface (it replaced v4's coherence prompt) and gets written and hand-evaluated first, before any plumbing.

## The loop (how a day works)

1. Talk to `voice:convo` from anywhere — phone PWA or any attached terminal. It converses; it does not grind.
2. **Plan stint** button → planner passthrough turns the conversation into a stint plan → you glance at it → it launches a workstream (Fable xhigh, `/role-root`).
3. Keep talking. **Send to workstream** routes your latest thinking through the injector into the running workstream. **Check in** has the convo session read a workstream's transcript tail and tell you where things stand, out loud.
4. Workstream permission gates surface on the phone as approve/deny cards (mechanism below) — or you just answer them in the terminal.
5. **Compact** button sends `/compact` to the convo session whenever you want the chat squeezed.

## Scope

### In (v5 first pass)
- tmux substrate module: spawn/inject/tail/close, session registry, spawn→transcript mapping
- `role-convo` skill + hand-run evaluation of its register and restraint
- Voice pipeline (kept from v4: Deepgram STT, VAD/endpointing, sentence-chunked Cartesia TTS, barge-in) rewired to the convo session; ambient Mac mic mode
- Server (kept skeleton: FastAPI, launchd, auth chain, SQLite) with the new WS protocol: chat stream, workstream cards, buttons
- PWA rework: persistent chat, Plan-stint / Send-to-workstream / Check-in / Compact buttons, workstream cards with live tails, approvals
- Planner + injector passthrough flows and their skills (`role-stint-plan`, `role-inject`)
- Approval surfacing as a **general phone-approval relay** — hit a hook → end up asking on the phone, whatever the user's hook setup looks like. The service exposes an approvals endpoint; voice-code ships one small client (`ask-phone`) that reads hook JSON on stdin, POSTs, waits for the verdict, and prints a permission decision. Workstream sessions get a PreToolUse hook wired to it out of the box, and a user's existing hooks (Ryan's bash gate, anyone's) can exec the same client. No verdict in time → the client prints no decision and Claude Code's native prompt takes over — that's built-in hook semantics, not fallback code we write.
- Deploy plugin updated: tmux install check, no Anthropic key step, bootstrap of the `voice` tmux session

### Out
- Any SDK/headless/API-loop path — **permanently**
- Sub-second latency as a goal. Fable low takes seconds to first token; that's the trade we're choosing. Streaming TTS off the transcript tail + instrumentation stay; a "thinking" earcon is the mitigation if silence feels dead.
- Native iOS app (PWA holds), Codex, cloud relay (permanently), multiple simultaneous *audio* attachments

## Latency stance

Measure, don't promise. Instrument endpoint → first-transcript-token → first-audio per turn (kept from v4). Barge-in still kills TTS instantly; the reply always lands in chat even when you talk over it. Expected feel: a thoughtful colleague, not a kiosk.

## Cleanup ledger (leftover v4 crap — delete, don't strand)

The elegance bar: after the pivot, a reader should find **no trace of the dual-layer design** except this brief's Context paragraph and git history.

**Delete outright**
- `voicecode/engine/` — Haiku loop, frozen prompt, dispatch parser (the sentence **chunker moves** to `voicecode/audio/`, it's TTS plumbing, not engine)
- `voicecode/adapters/claude_code.py`, `claude_code_distill.py`, `adapters/execution.py` — the SDK adapter and its ABC
- `voicecode/events.py` — the bridge vocabulary; UI state now derives from transcripts (a much smaller UI-message set lives in protocol.py)
- `evals/` — Haiku coherence evals are meaningless now; `role-convo` gets its own lighter eval script
- `protocol.py` messages that encoded the bridge (`Event`, dispatch-adjacent shapes); redesign around chat/workstream/button messages
- Tests of all the above (large); the two integration suites get rewritten against the tmux substrate with a fake tmux
- `pyproject.toml`: drop `anthropic` and `claude-agent-sdk` entirely
- AGENTS.md sections describing the engine/adapter/bridge behavior

**Keep (rewire)**
- `voicecode/audio/` pipeline, state machine, latency logging; `adapters/deepgram_stt.py`, `cartesia_tts.py`
- `voicecode/server/` auth (token+PIN+WebAuthn+credentials, scrypt contract), store (schema changes: sessions map to CC session ids + tmux windows; drop the duplicate transcript log — CC JSONL is the source of truth), static serving, launchd template
- PWA audio internals (worklet capture, gapless playback, pairing flow); the screens above it are rebuilt
- `skills/deploy/` + plugin scaffolding (edited, not rebuilt)

## Testing & Observability

- **Build optimistically**: one primary path per problem, no fallback code until testing — agent-run or Ryan's UAT — proves the primary path broken. Fallbacks that exist "just in case" are the leftover crap this brief exists to prevent.
- Substrate spike is milestone 0: prove inject (multiline, bracketed paste), transcript tail, session mapping, and the hook-based approval round-trip with throwaway scripts **before** the build.
- Unit tests mock at the tmux boundary (a `FakeTmux`) and use fixture JSONL transcripts; no test launches a real Claude Code session.
- One gated integration test that does drive a real `voice:convo` session end-to-end (text in → transcript out), run manually.
- Latency instrumentation and structured JSON logs carried over; `/healthz` unchanged.

## Deployment

`/voice-code:deploy` as before, minus the Anthropic key, plus: `tmux` presence (brew install), `voice` session bootstrap at service start (launchd service supervises the tmux session's existence, not the terminals). Rollback unchanged.

## Risks & Open Questions

- **Transcript JSONL is an undocumented internal format** — pin the parsing behind one module with defensive tests; expect breakage on Claude Code updates; the deploy skill should smoke-test it post-update.
- **TUI injection quirks** (input box state, paste bracketing, a session mid-permission-dialog swallowing input) — milestone-0 spike; queueing behavior of Claude Code's input box is the friend here.
- **Hook-based approvals** assume a hook can block on an external verdict with acceptable UX — proven or killed in the milestone-0 spike; nothing else gets built ahead of that answer.
- **role-convo efficiency** — the skill orients, it doesn't ban: use tools when they're the quick path, stay conversational otherwise, with success criteria stated plainly (small skill, like the other roles). Hand evals confirm register and pace before plumbing.
- **/compact and transcript continuity** — verify the JSONL tail survives compaction sanely.
