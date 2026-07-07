"""The single ConvoRuntime: one persistent conversation, one live phone socket.

Owns the convo bridge's entry→chat fan-out, the audio pipeline for whichever
socket is currently attached, and routes buttons to the workstream manager and
approvals. Chat sourcing rule: final chat renders from the CC transcript via
the fan-out; the pipeline's sink carries only state/audio/speech_end and STT
user interims.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from remote_workstreams import protocol
from remote_workstreams.adapters.stt import STTAdapter
from remote_workstreams.adapters.tts import TTSAdapter
from remote_workstreams.audio.state import PipelineState
from remote_workstreams.server.approvals import Approvals
from remote_workstreams.server.logs import log
from remote_workstreams.server.workstreams import WorkstreamManager
from remote_workstreams.transcript import AssistantText, CompactEnd, Entry, ToolActivity, UserText

logger = logging.getLogger("remote_workstreams.server.runtime")

STTFactory = Callable[[], STTAdapter]
TTSFactory = Callable[[], TTSAdapter]
# Kills the convo session, spawns a fresh one, re-points the bridge; returns the
# new transcript path. The composition root supplies it (it owns the concretes).
ConvoReset = Callable[[], Awaitable[Path]]


class ConvoBridge(Protocol):
    """The surface of remote_workstreams.convo.ConvoBridge the server uses (the concrete
    class is imported only by composition roots)."""

    def subscribe(self) -> AsyncIterator[Entry]: ...

    def history(self, limit: int = 200) -> list[Entry]: ...

    def turn(self, text: str) -> AsyncIterator[str]: ...

    async def slash(self, command: str) -> None: ...


class Pipeline(Protocol):
    """The AudioPipeline surface the server drives."""

    async def run(self) -> None: ...

    async def feed(self, pcm: bytes) -> None: ...

    async def text(self, text: str) -> None: ...

    def set_muted(self, muted: bool) -> None: ...

    async def close(self) -> None: ...


class PipelineFactory(Protocol):
    def __call__(
        self, stt: STTAdapter, tts: TTSAdapter, convo: ConvoBridge, sink: "ProtocolSink"
    ) -> Pipeline: ...


class ClientConnection(Protocol):
    """What the server needs from a connected client (ws.py implements it)."""

    async def send_message(self, message: object) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def close_with_error(self, message: str) -> None: ...


class ClientPush:
    """Server→client push point; drops silently when no phone is connected."""

    def __init__(self) -> None:
        self.conn: ClientConnection | None = None

    async def __call__(self, message: object) -> None:
        if self.conn is not None:
            await self.conn.send_message(message)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def entry_chat(entry: Entry) -> protocol.Chat | None:
    """CC transcript entry → chat frame; TurnEnd is not chat."""
    if isinstance(entry, UserText):
        return protocol.Chat(role="user", text=entry.text, ts=entry.ts, final=True)
    if isinstance(entry, AssistantText):
        return protocol.Chat(role="assistant", text=entry.text, ts=entry.ts, final=True)
    if isinstance(entry, ToolActivity):
        return protocol.Chat(role="activity", text=entry.label, ts=entry.ts, final=True)
    return None


class ProtocolSink:
    """AudioSink → protocol frames. transcript() carries STT user interims only."""

    def __init__(self, conn: ClientConnection) -> None:
        self.conn = conn

    async def state(self, state: PipelineState) -> None:
        await self.conn.send_message(protocol.State(state=state.value))

    async def transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None:
        await self.conn.send_message(protocol.Chat(role=role, text=text, ts=_now(), final=final))

    async def audio(self, pcm: bytes) -> None:
        await self.conn.send_audio(pcm)

    async def speech_end(self) -> None:
        await self.conn.send_message(protocol.SpeechEnd())


class ConvoRuntime:
    def __init__(
        self,
        bridge: ConvoBridge,
        push: ClientPush,
        workstreams: WorkstreamManager,
        approvals: Approvals,
        *,
        stt_factory: STTFactory,
        tts_factory: TTSFactory,
        pipeline_factory: PipelineFactory,
        convo_reset: ConvoReset,
    ) -> None:
        self.bridge = bridge
        self.push = push
        self.workstreams = workstreams
        self.approvals = approvals
        self.stt_factory = stt_factory
        self.tts_factory = tts_factory
        self.pipeline_factory = pipeline_factory
        self.convo_reset = convo_reset
        self.conn: ClientConnection | None = None
        self.pipeline: Pipeline | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._fanout_task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self._button_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self._fanout_task = asyncio.create_task(self._fan_out())
        self._status_task = asyncio.create_task(self.workstreams.run())

    async def shutdown(self) -> None:
        for task in (self._fanout_task, self._status_task, *self._button_tasks):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        async with self._lock:
            await self._teardown()

    # ---- socket lifecycle ----

    async def attach(self, conn: ClientConnection) -> None:
        """Takeover semantics: one live socket globally. Replays chat history to
        the new socket, then live entries stream via the fan-out."""
        async with self._lock:
            if self.conn is not None:
                await self._teardown(error="another connection took over")
            for entry in self.bridge.history():
                chat = entry_chat(entry)
                if chat is not None:
                    await conn.send_message(chat)
            self.conn = conn
            self.push.conn = conn
            sink = ProtocolSink(conn)
            self.pipeline = self.pipeline_factory(
                self.stt_factory(), self.tts_factory(), self.bridge, sink
            )
            self._pipeline_task = asyncio.create_task(self._run_pipeline(self.pipeline))
            log(logger, "client_attached")

    async def detach(self, conn: ClientConnection) -> None:
        async with self._lock:
            if self.conn is conn:
                await self._teardown()
                log(logger, "client_detached")

    # ---- buttons ----

    def new_workstream(self) -> None:
        self._background(self.workstreams.new_workstream())

    def send_to_workstream(self, name: str) -> None:
        self._background(self.workstreams.send_to_workstream(name))

    def end_workstream(self, name: str) -> None:
        self._background(self.workstreams.end_workstream(name))

    async def check_in(self, name: str) -> None:
        path = self.workstreams.transcript_path(name)
        if path is None:
            await self.push(protocol.Error(message=f"unknown workstream: {name}"))
            return
        directive = (
            f"Check in on workstream {name}: read the tail of {path}"
            " and tell me where things stand."
        )
        if self.pipeline is not None:
            await self.pipeline.text(directive)

    async def compact(self) -> None:
        await self.bridge.slash("/compact")

    def compact_workstream(self, name: str) -> None:
        self._background(self.workstreams.compact_workstream(name))

    def clear_convo(self) -> None:
        self._background(self._clear_convo())

    async def _clear_convo(self) -> None:
        # Planners and injectors read the convo transcript; a fresh session means
        # a fresh file, so the manager must follow.
        self.workstreams.convo_transcript = await self.convo_reset()
        await self.push(protocol.ConvoCleared())
        log(logger, "convo_cleared")

    # ---- internals ----

    def _background(self, coro: Coroutine) -> None:
        task = asyncio.create_task(coro)
        self._button_tasks.add(task)
        task.add_done_callback(self._button_tasks.discard)

    async def _fan_out(self) -> None:
        async for entry in self.bridge.subscribe():
            if isinstance(entry, CompactEnd):
                await self.push(protocol.Compacted())  # stops the phone's spinner
                continue
            chat = entry_chat(entry)
            if chat is not None:
                await self.push(chat)

    async def _teardown(self, error: str | None = None) -> None:
        conn, self.conn, self.push.conn = self.conn, None, None
        pipeline, self.pipeline = self.pipeline, None
        task, self._pipeline_task = self._pipeline_task, None
        if pipeline is not None:
            with contextlib.suppress(Exception):
                await pipeline.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if conn is not None and error is not None:
            await conn.close_with_error(error)

    @staticmethod
    async def _run_pipeline(pipeline: Pipeline) -> None:
        try:
            await pipeline.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pipeline run failed")
