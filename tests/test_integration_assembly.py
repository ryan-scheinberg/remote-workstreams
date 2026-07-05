"""The assembled system: real create_app + real ConvoBridge over a growing JSONL +
real AudioPipeline, faked only at the tmux/STT/TTS boundaries, driven over a real
WebSocket (TestClient). Covers the cross-module seams: transcript-sourced chat and
turn streaming, barge-in, the workstream lifecycle, approvals mid-turn, /compact
continuity, and hooks/ask_phone.py as a real subprocess against a live HTTP+WS server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.testclient import TestClient
from websockets.sync.client import connect

from server_fakes import Fakes, FakeSubstrate, make_app, seed_session
from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.pipeline import AudioPipeline
from voicecode.config import Config
from voicecode.convo import ConvoBridge
from voicecode.protocol import Approval, Compact, Hello, NewWorkstream, TextInput
from voicecode.server.app import create_app
from voicecode.server.store import Store
from voicecode.substrate import CCSession, SessionSpec

TS = "2026-07-03T10:00:00.000Z"

ASK_PHONE = Path(__file__).resolve().parent.parent / "hooks" / "ask_phone.py"


def user_line(text: str) -> str:
    return json.dumps(
        {"type": "user", "timestamp": TS, "message": {"role": "user", "content": text}}
    )


def assistant_line(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": TS,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def turn_end_line() -> str:
    return json.dumps(
        {"type": "system", "subtype": "turn_duration", "timestamp": TS, "durationMs": 42}
    )


def append(path: Path, *lines: str) -> None:
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


class ScriptedSTT(STTAdapter):
    """Thread-safe scripted STT: the test thread pushes chunks into the app's loop."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TranscriptChunk | None] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

    def push(self, text: str, *, is_final: bool = False, speech_final: bool = False) -> None:
        chunk = TranscriptChunk(text=text, is_final=is_final, speech_final=speech_final)
        assert self.loop is not None, "pipeline not running yet"
        self.loop.call_soon_threadsafe(self._queue.put_nowait, chunk)

    async def stream(self, audio):
        self.loop = asyncio.get_running_loop()
        consume = asyncio.create_task(self._consume(audio))
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                yield chunk
        finally:
            consume.cancel()

    async def _consume(self, audio) -> None:
        async for _pcm in audio:
            pass
        self._queue.put_nowait(None)  # mic ended -> transcript stream ends


class MarkerTTS(TTSAdapter):
    """PCM = b"PCM:" + sentence, so binary frames identify what was spoken.
    hold_first parks the first synthesize mid-utterance for barge-in tests."""

    def __init__(self, hold_first: bool = False) -> None:
        self.synthesized: list[str] = []
        self.cancels = 0
        self._hold = asyncio.Event() if hold_first else None

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield b"PCM:" + text.encode()
        if self._hold is not None and len(self.synthesized) == 1:
            await self._hold.wait()

    async def cancel(self) -> None:
        self.cancels += 1


class Rig:
    """Everything a test needs from the assembled app."""

    def __init__(self, client, transcript: Path, substrate: FakeSubstrate, bridge, stts, ttss):
        self.client = client
        self.transcript = transcript
        self.substrate = substrate
        self.bridge = bridge
        self.stts = stts
        self.ttss = ttss

    @property
    def stt(self) -> ScriptedSTT:
        return self.stts[-1]


@contextmanager
def assembled(tmp_path: Path, hold_tts: bool = False):
    """create_app on the real bridge/pipeline; the composition root's bridge.run()
    task is reproduced by wrapping the app lifespan, exactly like __main__ does."""
    transcript = tmp_path / "transcripts" / "convo-id.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    substrate = FakeSubstrate(tmp_path / "transcripts")
    session = CCSession(
        session_id="convo-id",
        window="voice:convo",
        transcript=transcript,
        spec=SessionSpec(name="convo", model="fable", effort="low", display_name="convo"),
    )
    bridge = ConvoBridge(substrate, session, poll_interval=0.01)
    stts: list[ScriptedSTT] = []
    ttss: list[MarkerTTS] = []

    def stt_factory() -> ScriptedSTT:
        stts.append(ScriptedSTT())
        return stts[-1]

    def tts_factory() -> MarkerTTS:
        ttss.append(MarkerTTS(hold_first=hold_tts))
        return ttss[-1]

    async def convo_reset() -> Path:
        raise AssertionError("clear_convo is not under test here")

    config = Config(data_dir=tmp_path / "data")
    app = create_app(
        config,
        store=Store(config.db_path),
        bridge=bridge,
        substrate=substrate,
        convo_transcript=transcript,
        stt_factory=stt_factory,
        tts_factory=tts_factory,
        pipeline_factory=AudioPipeline,
        convo_reset=convo_reset,
        approvals_token="boot-token",
        plugin_dir=tmp_path / "plugin",
        workstream_settings=tmp_path / "workstream-settings.json",
    )
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(bridge.run())
        async with inner(app):
            yield
        await bridge.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        seed_session(app.state, "cred-1")
        app.state.runtime.workstreams._poll_interval = 0.02  # file polls, test speed
        yield Rig(client, transcript, substrate, bridge, stts, ttss)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def hello(ws, credential="cred-1") -> dict:
    ws.send_text(Hello(credential=credential).model_dump_json())
    return json.loads(ws.receive_text())


def frame(ws) -> dict | bytes:
    """Next WS frame: decoded JSON for text frames, raw bytes for TTS audio."""
    message = ws.receive()
    if message.get("text") is not None:
        return json.loads(message["text"])
    return message["bytes"]


def collect_until(ws, done, limit=50) -> list[dict | bytes]:
    frames: list[dict | bytes] = []
    for _ in range(limit):
        f = frame(ws)
        frames.append(f)
        if isinstance(f, dict) and done(f):
            return frames
    raise AssertionError(f"frame limit hit before condition; got {frames}")


def chats(frames) -> list[tuple[str, str, bool]]:
    return [
        (f["role"], f["text"], f["final"])
        for f in frames
        if isinstance(f, dict) and f.get("type") == "chat"
    ]


def audio(frames) -> list[bytes]:
    return [f for f in frames if isinstance(f, bytes)]


def states(frames) -> list[str]:
    return [f["state"] for f in frames if isinstance(f, dict) and f.get("type") == "state"]


# ---- seam 1: real bridge + real pipeline inside the real app ----


def test_text_turn_streams_chat_audio_and_states(tmp_path):
    with assembled(tmp_path) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        ws.send_text(TextInput(text="what's next").model_dump_json())
        wait_for(lambda: ("voice:convo", "what's next") in rig.substrate.sent)
        # Claude Code echoes the input as a user line, replies, then ends the turn
        append(
            rig.transcript,
            user_line("what's next"),
            assistant_line("Shipping the store today. Then tests after that."),
            turn_end_line(),
        )
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        assert states(frames) == ["thinking", "speaking", "listening"]
        assert chats(frames) == [
            ("user", "what's next", True),
            ("assistant", "Shipping the store today. Then tests after that.", True),
        ]
        assert audio(frames) == [
            b"PCM:Shipping the store today.",
            b"PCM:Then tests after that.",
        ]
        assert any(isinstance(f, dict) and f.get("type") == "speech_end" for f in frames)


def test_history_replay_matches_on_takeover(tmp_path):
    with assembled(tmp_path) as rig:
        append(
            rig.transcript,
            user_line("earlier question"),
            assistant_line("Earlier answer."),
            turn_end_line(),
        )
        wait_for(lambda: rig.bridge._tail._offset > 0)  # fan-out consumed, no client yet
        with rig.client.websocket_connect("/ws") as ws1:
            assert hello(ws1)["type"] == "ready"
            replay1 = [json.loads(ws1.receive_text()) for _ in range(2)]  # TurnEnd is not chat
            with rig.client.websocket_connect("/ws") as ws2:
                assert hello(ws2)["type"] == "ready"
                replay2 = [json.loads(ws2.receive_text()) for _ in range(2)]
                assert json.loads(ws1.receive_text()) == {
                    "type": "error", "message": "another connection took over",
                }
                assert replay1 == replay2
                assert [(f["role"], f["text"]) for f in replay2] == [
                    ("user", "earlier question"), ("assistant", "Earlier answer."),
                ]


def test_stt_interims_then_transcript_final_no_duplicates(tmp_path):
    """Chat-sourcing rule across the seam: the pipeline emits user interims
    (final=False) only; the single final user line comes from the transcript."""
    with assembled(tmp_path) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        wait_for(lambda: rig.stts and rig.stt.loop is not None)
        rig.stt.push("fix the")
        interim = json.loads(ws.receive_text())
        assert (interim["role"], interim["text"], interim["final"]) == ("user", "fix the", False)
        rig.stt.push("fix the login bug", is_final=True, speech_final=True)
        wait_for(lambda: ("voice:convo", "fix the login bug") in rig.substrate.sent)
        append(
            rig.transcript,
            user_line("fix the login bug"),
            assistant_line("On it, checking auth now."),
            turn_end_line(),
        )
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        user_chats = [c for c in chats(frames) if c[0] == "user"]
        assert user_chats == [("user", "fix the login bug", True)]  # exactly one, transcript-final


# ---- seam 3: barge-in against the real bridge ----


def test_barge_in_keeps_chat_and_next_turn_speaks(tmp_path):
    with assembled(tmp_path, hold_tts=True) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        wait_for(lambda: rig.stts and rig.stt.loop is not None)
        rig.stt.push("start the demo", is_final=True, speech_final=True)
        wait_for(lambda: ("voice:convo", "start the demo") in rig.substrate.sent)
        # the full reply is in the transcript; TTS parks mid-first-sentence
        full_reply = "A long first sentence to interrupt. And then a second one."
        append(rig.transcript, user_line("start the demo"), assistant_line(full_reply))
        frames = collect_until(ws, lambda f: f.get("state") == "speaking")

        rig.stt.push("wait")  # non-empty interim while SPEAKING = barge-in
        frames += collect_until(ws, lambda f: f.get("state") == "interrupted")
        assert rig.ttss[-1].cancels >= 1  # TTS killed instantly
        # the fan-out still delivered the full assistant text to chat
        assert ("assistant", full_reply, True) in chats(frames)

        # next turn: mid-turn input queues (Milestone-0), so the interrupted turn's
        # leftovers and TurnEnd land first, then the new reply
        rig.stt.push("wait stop that", is_final=True, speech_final=True)
        wait_for(lambda: ("voice:convo", "wait stop that") in rig.substrate.sent)
        append(
            rig.transcript,
            assistant_line("Leftover tail of turn one."),
            turn_end_line(),
            user_line("wait stop that"),
            assistant_line("Okay, stopping and switching gears."),
            turn_end_line(),
        )
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        assert audio(frames) == [b"PCM:Okay, stopping and switching gears."]  # not the leftovers
        assert any(isinstance(f, dict) and f.get("type") == "speech_end" for f in frames)


# ---- seams 4 + 2: workstream lifecycle and check-in over the live socket ----


def test_new_workstream_and_check_in_over_live_ws(tmp_path):
    with assembled(tmp_path) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        append(rig.transcript, user_line("let's plan the day"), assistant_line("Sure."),
               turn_end_line())
        collect_until(ws, lambda f: f.get("type") == "chat" and f["role"] == "assistant")

        plan_text = "Stint: Ship the QA suite\n\nGoal: coverage.\n"
        ws.send_text(NewWorkstream().model_dump_json())
        wait_for(lambda: any(s.spec.name == "plan" for s in rig.substrate.spawned))
        planner = next(s for s in rig.substrate.spawned if s.spec.name == "plan")
        prompt = planner.spec.initial_prompt
        assert f"convo={rig.transcript}" in prompt and "since_line=0" in prompt
        Path(prompt.split("output=")[1]).write_text(plan_text)  # the planner's job

        # The plan launches straight into a workstream — no review card in between.
        wait_for(lambda: any(s.spec.name.startswith("ws-") for s in rig.substrate.spawned))
        ws_session = next(s for s in rig.substrate.spawned if s.spec.name.startswith("ws-"))
        wait_for(lambda: (ws_session.window, plan_text) in rig.substrate.sent)  # plan pasted
        assert "voice:plan" in rig.substrate.killed
        assert (ws_session.spec.model, ws_session.spec.effort) == ("fable", "xhigh")
        card = collect_until(ws, lambda f: f.get("type") == "workstreams")[-1]["workstreams"][0]
        assert (card["name"], card["status"]) == ("ws-ship-the-qa-suite", "running")

        ws.send_text(json.dumps({"type": "check_in", "workstream": card["name"]}))
        directive = (
            f"Check in on workstream {card['name']}: read the tail of"
            f" {ws_session.transcript} and tell me where things stand."
        )
        wait_for(lambda: ("voice:convo", directive) in rig.substrate.sent)
        append(rig.transcript, user_line(directive),
               assistant_line("The workstream just finished the store."), turn_end_line())
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        assert audio(frames) == [b"PCM:The workstream just finished the store."]  # spoken aloud


def test_approval_round_trip_while_turn_in_flight(tmp_path):
    with assembled(tmp_path) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        ws.send_text(TextInput(text="keep going").model_dump_json())
        collect_until(ws, lambda f: f.get("state") == "thinking")  # turn is in flight
        result = {}

        def post():
            result["response"] = rig.client.post(
                "/approvals",
                json={"session_id": "ws-1", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}},
                headers={"X-Voicecode-Token": "boot-token"},
            )

        thread = threading.Thread(target=post)
        thread.start()
        request = collect_until(ws, lambda f: f.get("type") == "approval_request")[-1]
        assert (request["tool"], request["summary"]) == ("Bash", "git push --force")
        ws.send_text(
            Approval(approval_id=request["approval_id"], approved=True).model_dump_json()
        )
        thread.join(timeout=5)
        assert result["response"].json() == {"decision": "allow"}
        # the in-flight turn still completes normally
        append(rig.transcript, user_line("keep going"), assistant_line("Continuing."),
               turn_end_line())
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        assert audio(frames) == [b"PCM:Continuing."]


# ---- seam 6: /compact continuity ----


def test_compact_continuity(tmp_path):
    with assembled(tmp_path) as rig, rig.client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        ws.send_text(Compact().model_dump_json())
        wait_for(lambda: ("voice:convo", "/compact") in rig.substrate.sent)
        # /compact appends meta lines to the SAME file (Milestone-0); none are chat
        append(
            rig.transcript,
            json.dumps({"type": "user", "timestamp": TS, "message": {
                "role": "user", "content": "<command-name>/compact</command-name>"}}),
            json.dumps({"type": "summary", "summary": "compacted", "leafUuid": "x"}),
            json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": TS}),
        )
        ws.send_text(TextInput(text="still with me?").model_dump_json())
        wait_for(lambda: ("voice:convo", "still with me?") in rig.substrate.sent)
        append(rig.transcript, user_line("still with me?"), assistant_line("Right here."),
               turn_end_line())
        frames = collect_until(ws, lambda f: f.get("state") == "listening")
        assert chats(frames) == [
            ("user", "still with me?", True),
            ("assistant", "Right here.", True),
        ]


# ---- seam 5: the real ask_phone.py subprocess against a live server ----


@pytest.fixture
def live_server(tmp_path):
    """The real app under real uvicorn on an ephemeral port (ask_phone needs HTTP)."""
    fakes = Fakes(tmp_path)
    app = make_app(tmp_path, fakes)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    seed_session(app.state, "cred-1")
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    thread.join(timeout=5)


def run_ask_phone(port: int, payload: dict, *flags: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(ASK_PHONE), "--port", str(port), "--token", "boot-token",
         "--wait", "30", *flags],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()
    return proc


def test_ask_phone_relays_verdict_through_real_server(live_server):
    port = live_server
    payload = {"session_id": "ws-1", "tool_name": "Bash",
               "tool_input": {"command": "git reset --hard origin/main"}}
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:
        ws.send(json.dumps({"type": "hello", "credential": "cred-1"}))
        assert json.loads(ws.recv(timeout=10))["type"] == "ready"
        for approved, decision in ((True, "allow"), (False, "deny")):
            proc = run_ask_phone(port, payload, "--gate-bash")
            request = json.loads(ws.recv(timeout=10))
            assert request["type"] == "approval_request"
            assert request["summary"] == "git reset --hard origin/main"
            ws.send(json.dumps({"type": "approval", "approval_id": request["approval_id"],
                                "approved": approved}))
            proc.wait(timeout=15)
            verdict = json.loads(proc.stdout.read())["hookSpecificOutput"]
            assert verdict["permissionDecision"] == decision
            assert verdict["hookEventName"] == "PreToolUse"


def test_ask_phone_gate_bash_skips_safe_command_end_to_end(live_server):
    proc = run_ask_phone(live_server, {"session_id": "s", "tool_name": "Bash",
                                       "tool_input": {"command": "git status"}}, "--gate-bash")
    proc.wait(timeout=15)
    assert proc.stdout.read() == ""  # never reached the relay; tool proceeds natively
