"""Fakes injected through create_app's DI — server tests never touch live APIs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from voicecode.adapters.execution import ExecutionAdapter
from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.state import PipelineState
from voicecode.config import Config
from voicecode.events import Progress, StatusEvent, TaskStarted
from voicecode.server.app import create_app


class FakeEngine:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.loaded: list[dict[str, Any]] | None = None
        self.injected: list[StatusEvent] = []

    def inject_events(self, events) -> None:
        self.injected.extend(events)

    def export_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        self.loaded = list(messages)
        self.messages = list(messages)


class FakeExecution(ExecutionAdapter):
    """Emits a TaskStarted on start() and a Progress on send() into its event stream."""

    def __init__(self) -> None:
        self.started_prompts: list[str] = []
        self.sent: list[str] = []
        self.approvals: list[tuple[str, bool]] = []
        self.resumed: list[str] = []
        self.stopped = False
        self._queue: asyncio.Queue[StatusEvent | None] = asyncio.Queue()

    async def start(self, prompt: str) -> str:
        self.started_prompts.append(prompt)
        self._queue.put_nowait(TaskStarted(summary="Execution started."))
        return "exec-1"

    async def send(self, message: str) -> None:
        self.sent.append(message)
        self._queue.put_nowait(Progress(summary="Still working."))

    async def events(self) -> AsyncIterator[StatusEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def resume(self, session_id: str) -> None:
        self.resumed.append(session_id)

    async def approve(self, gate_id: str, approved: bool) -> None:
        self.approvals.append((gate_id, approved))

    async def stop(self) -> None:
        self.stopped = True
        self._queue.put_nowait(None)

    def push_event(self, event: StatusEvent) -> None:
        self._queue.put_nowait(event)


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


class FakePipeline:
    """Implements the frozen AudioPipeline surface. text() emulates one committed
    turn so the sink mapping and persistence paths are exercised; a text starting
    with "dispatch:" routes the rest through on_dispatch."""

    def __init__(self, stt, tts, engine, sink, on_dispatch=None) -> None:
        self.engine = engine
        self.sink = sink
        self.on_dispatch = on_dispatch
        self.fed: list[bytes] = []
        self.texts: list[str] = []
        self.events: list[StatusEvent] = []
        self.muted = False
        self.closed = False
        self._done = asyncio.Event()

    async def run(self) -> None:
        await self._done.wait()

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def text(self, text: str) -> None:
        self.texts.append(text)
        reply = f"echo: {text}"
        self.engine.messages.append({"role": "user", "content": text})
        self.engine.messages.append({"role": "assistant", "content": reply})
        await self.sink.state(PipelineState.THINKING)
        await self.sink.transcript("user", text, True)
        await self.sink.transcript("assistant", reply, True)
        await self.sink.audio(b"\x01\x02")
        await self.sink.speech_end()
        await self.sink.state(PipelineState.LISTENING)
        if self.on_dispatch is not None and text.startswith("dispatch:"):
            await self.on_dispatch(text.removeprefix("dispatch:").strip())

    async def on_events(self, events) -> None:
        self.events.extend(events)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    async def close(self) -> None:
        self.closed = True
        self._done.set()


class Fakes:
    """Factory registry: create_app collaborators that record every instance."""

    def __init__(self) -> None:
        self.engines: list[FakeEngine] = []
        self.executions: list[FakeExecution] = []
        self.pipelines: list[FakePipeline] = []

    def engine_factory(self) -> FakeEngine:
        engine = FakeEngine()
        self.engines.append(engine)
        return engine

    def execution_factory(self) -> FakeExecution:
        execution = FakeExecution()
        self.executions.append(execution)
        return execution

    def stt_factory(self) -> FakeSTT:
        return FakeSTT()

    def tts_factory(self) -> FakeTTS:
        return FakeTTS()

    def pipeline_factory(self, stt, tts, engine, sink, on_dispatch=None) -> FakePipeline:
        pipeline = FakePipeline(stt, tts, engine, sink, on_dispatch)
        self.pipelines.append(pipeline)
        return pipeline


class FakeConn:
    """sessions.ClientConnection for manager-level tests."""

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


def make_app(tmp_path: Path, fakes: Fakes, web_dir: Path | None = None):
    config = Config(data_dir=tmp_path / "data")
    return create_app(
        config,
        engine_factory=fakes.engine_factory,
        execution_factory=fakes.execution_factory,
        stt_factory=fakes.stt_factory,
        tts_factory=fakes.tts_factory,
        pipeline_factory=fakes.pipeline_factory,
        web_dir=web_dir,
    )
