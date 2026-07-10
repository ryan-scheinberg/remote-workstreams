"""Fakes injected through create_app's DI — server tests never touch live APIs or
tmux, and never import remote_workstreams.convo (built in parallel; FakeConvoBridge stands
in for its exact interface).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from remote_workstreams.adapters.stt import STTAdapter, TranscriptChunk
from remote_workstreams.adapters.tts import TTSAdapter
from remote_workstreams.audio.state import PipelineState
from remote_workstreams.config import Config
from remote_workstreams.server.app import create_app
from remote_workstreams.server.store import Store
from remote_workstreams.substrate import CCSession, SessionSpec
from remote_workstreams.transcript import Entry


class FakeSTT(STTAdapter):
    async def stream(self, audio) -> AsyncIterator[TranscriptChunk]:
        return
        yield


class FakeTTS(TTSAdapter):
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        return
        yield

    async def cancel(self) -> None:
        pass


class FakeConvoBridge:
    """Same surface as remote_workstreams.convo.ConvoBridge; records everything."""

    def __init__(self) -> None:
        self.history_entries: list[Entry] = []
        self.sent: list[str] = []
        self.turns: list[str] = []
        self.slashes: list[str] = []
        self.closed = False
        self._subscribers: list[asyncio.Queue[Entry]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self) -> None:
        await asyncio.Event().wait()

    def subscribe(self) -> AsyncIterator[Entry]:
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Entry] = asyncio.Queue()
        self._subscribers.append(queue)

        async def entries() -> AsyncIterator[Entry]:
            while True:
                yield await queue.get()

        return entries()

    def push_entry(self, entry: Entry) -> None:
        """Thread-safe: TestClient-based tests call this from the test thread."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for queue in self._subscribers:
            if self._loop is not None and current is not self._loop:
                self._loop.call_soon_threadsafe(queue.put_nowait, entry)
            else:
                queue.put_nowait(entry)

    def history(self, limit: int = 200) -> list[Entry]:
        return self.history_entries[-limit:]

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def turn(self, text: str) -> AsyncIterator[str]:
        self.turns.append(text)

        async def chunks() -> AsyncIterator[str]:
            yield f"echo: {text}"

        return chunks()

    async def slash(self, command: str) -> None:
        self.slashes.append(command)

    async def close(self) -> None:
        self.closed = True


class FakeSubstrate:
    """Records spawns/sends/kills; aliveness is scripted via alive_windows."""

    def __init__(self, transcript_dir: Path) -> None:
        self.transcript_dir = transcript_dir
        self.spawned: list[CCSession] = []
        self.sent: list[tuple[str, str]] = []
        self.killed: list[str] = []
        self.archived: list[str] = []
        self.alive_windows: set[str] = set()

    def codex_transcript(self, session_id: str) -> Path:
        return self.transcript_dir / f"{session_id}.jsonl"

    async def spawn(self, spec: SessionSpec, session_id: str | None = None) -> CCSession:
        if spec.resume and session_id is None:
            raise ValueError("resume requires the existing session_id")
        session_id = session_id or str(uuid.uuid4())
        session = CCSession(
            session_id=session_id,
            window=f"voice:{spec.name}",
            transcript=self.transcript_dir / f"{session_id}.jsonl",
            spec=spec,
        )
        # A spawned fake is a booted session: its transcript opens with the role
        # greeting (what _await_ready keys on in the real system), in the format
        # of the spec's engine.
        session.transcript.parent.mkdir(parents=True, exist_ok=True)
        if spec.engine == "codex":
            greeting = {"type": "event_msg", "payload": {"type": "agent_message", "message": "Ready."}}
        else:
            greeting = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Ready."}]}}
        session.transcript.write_text(json.dumps(greeting) + "\n")
        self.spawned.append(session)
        self.alive_windows.add(session.window)
        return session

    async def send(self, session: CCSession, text: str) -> None:
        self.sent.append((session.window, text))

    async def slash(self, session: CCSession, command: str) -> None:
        self.sent.append((session.window, command))

    async def rename(self, session: CCSession, name: str) -> None:
        if session.spec.engine == "codex":
            self.sent.append((session.window, f"/rename {name}"))

    async def archive(self, session: CCSession) -> None:
        if session.spec.engine == "codex":
            self.archived.append(session.session_id)

    async def alive(self, session: CCSession) -> bool:
        return session.window in self.alive_windows

    async def kill(self, session: CCSession) -> None:
        self.alive_windows.discard(session.window)
        self.killed.append(session.window)


class FakePipeline:
    """Implements the pipeline surface runtime.py drives. text() runs one turn
    through convo.turn() and plays it back through the sink."""

    def __init__(self, stt, tts, convo, sink) -> None:
        self.convo = convo
        self.sink = sink
        self.fed: list[bytes] = []
        self.texts: list[str] = []
        self.muted = False
        self.hushed = False
        self.closed = False
        self._done = asyncio.Event()

    async def run(self) -> None:
        await self._done.wait()

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def text(self, text: str) -> None:
        self.texts.append(text)
        await self.sink.state(PipelineState.THINKING)
        async for _sentence in self.convo.turn(text):
            await self.sink.audio(b"\x01\x02")
        await self.sink.speech_end()
        await self.sink.state(PipelineState.LISTENING)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def set_hushed(self, hushed: bool) -> None:
        self.hushed = hushed

    async def close(self) -> None:
        self.closed = True
        self._done.set()


class FakeConn:
    """runtime.ClientConnection for direct runtime/approvals tests."""

    def __init__(self) -> None:
        self.messages: list[object] = []
        self.audio: list[bytes] = []
        self.closed_error: str | None = None

    async def send_message(self, message: object) -> None:
        self.messages.append(message)

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def close_with_error(self, message: str) -> None:
        self.closed_error = message


class Fakes:
    """create_app collaborators; every pipeline instance is recorded."""

    def __init__(self, tmp_path: Path) -> None:
        self.bridge = FakeConvoBridge()
        self.substrate = FakeSubstrate(tmp_path / "transcripts")
        self.pipelines: list[FakePipeline] = []
        self.convo_resets = 0
        self.fresh_transcript = tmp_path / "convo-fresh.jsonl"

    def stt_factory(self) -> FakeSTT:
        return FakeSTT()

    def tts_factory(self) -> FakeTTS:
        return FakeTTS()

    def pipeline_factory(self, stt, tts, convo, sink) -> FakePipeline:
        pipeline = FakePipeline(stt, tts, convo, sink)
        self.pipelines.append(pipeline)
        return pipeline

    async def convo_reset(self) -> Path:
        self.convo_resets += 1
        return self.fresh_transcript


def seed_session(state, token: str = "cred-1") -> str:
    """Admit a known token as a live session — tests skip the WebAuthn ceremony.
    `state` is app.state."""
    from remote_workstreams.server import auth

    state.login._sessions[auth._session_hash(token)] = time.time() + auth.SESSION_TTL_SECONDS
    return token


def make_app(tmp_path: Path, fakes: Fakes, web_dir: Path | None = None):
    config = Config(data_dir=tmp_path / "data")
    store = Store(config.db_path)
    return create_app(
        config,
        store=store,
        bridge=fakes.bridge,
        substrate=fakes.substrate,
        convo_transcript=tmp_path / "convo.jsonl",
        stt_factory=fakes.stt_factory,
        tts_factory=fakes.tts_factory,
        pipeline_factory=fakes.pipeline_factory,
        convo_reset=fakes.convo_reset,
        approvals_token="boot-token",
        plugin_dir=tmp_path / "plugin",
        workstream_settings=tmp_path / "workstream-settings.json",
        web_dir=web_dir,
    )
