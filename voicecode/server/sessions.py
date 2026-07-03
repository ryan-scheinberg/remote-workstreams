"""Server-side session runtimes. Disconnect detaches the socket; the runtime
(engine, execution adapter, buffered events) stays alive and every committed
turn is already persisted, so reconnect — or a server restart — resumes.

One live attached audio session at a time: a new connection takes over and the
old socket gets Error + close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from voicecode import protocol
from voicecode.adapters.execution import ExecutionAdapter
from voicecode.adapters.stt import STTAdapter
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.pipeline import AudioPipeline, AudioSink
from voicecode.audio.state import PipelineState
from voicecode.engine.conversation import ConversationEngine
from voicecode.events import StatusEvent
from voicecode.server.logs import log
from voicecode.server.store import SessionRow, Store

logger = logging.getLogger("voicecode.server.sessions")

EngineFactory = Callable[[], ConversationEngine]
ExecutionFactory = Callable[[], ExecutionAdapter]
STTFactory = Callable[[], STTAdapter]
TTSFactory = Callable[[], TTSAdapter]
OnDispatch = Callable[[str], Awaitable[None]]


class PipelineFactory(Protocol):
    def __call__(
        self,
        stt: STTAdapter,
        tts: TTSAdapter,
        engine: ConversationEngine,
        sink: AudioSink,
        on_dispatch: OnDispatch,
    ) -> AudioPipeline: ...


class ClientConnection(Protocol):
    """What the server needs from a connected client (ws.py implements it)."""

    async def send_message(self, message: object) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def close_with_error(self, message: str) -> None: ...


class UnknownSession(Exception):
    pass


class SessionRuntime:
    def __init__(self, session_id: str, engine: ConversationEngine, execution: ExecutionAdapter):
        self.session_id = session_id
        self.engine = engine
        self.execution = execution
        self.execution_session_id: str | None = None
        self.execution_started = False
        self.pending_events: list[StatusEvent] = []  # buffered while no client attached
        self.pipeline: AudioPipeline | None = None
        self.conn: ClientConnection | None = None
        self.run_task: asyncio.Task | None = None
        self.pump_task: asyncio.Task | None = None


class ProtocolSink:
    """AudioSink → protocol messages + binary TTS frames; persists committed turns."""

    def __init__(self, conn: ClientConnection, store: Store, runtime: SessionRuntime) -> None:
        self.conn = conn
        self.store = store
        self.runtime = runtime

    async def state(self, state: PipelineState) -> None:
        await self.conn.send_message(protocol.State(state=state.value))

    async def transcript(
        self, role: Literal["user", "assistant"], text: str, final: bool
    ) -> None:
        await self.conn.send_message(protocol.Transcript(role=role, text=text, final=final))
        if not final:
            return
        self.store.add_transcript(self.runtime.session_id, role, text)
        if role == "user":
            self.store.set_title_if_default(self.runtime.session_id, text[:60])
        persist_messages(self.store, self.runtime)

    async def audio(self, pcm: bytes) -> None:
        await self.conn.send_audio(pcm)

    async def speech_end(self) -> None:
        await self.conn.send_message(protocol.SpeechEnd())


def persist_messages(store: Store, runtime: SessionRuntime) -> None:
    try:
        messages = runtime.engine.export_messages()
    except NotImplementedError:
        return
    except Exception:
        logger.exception("export_messages failed")
        return
    store.save_messages(runtime.session_id, messages)


class SessionManager:
    def __init__(
        self,
        store: Store,
        *,
        engine_factory: EngineFactory,
        execution_factory: ExecutionFactory,
        stt_factory: STTFactory,
        tts_factory: TTSFactory,
        pipeline_factory: PipelineFactory,
    ) -> None:
        self.store = store
        self.engine_factory = engine_factory
        self.execution_factory = execution_factory
        self.stt_factory = stt_factory
        self.tts_factory = tts_factory
        self.pipeline_factory = pipeline_factory
        self.runtimes: dict[str, SessionRuntime] = {}
        self.live: SessionRuntime | None = None
        self._lock = asyncio.Lock()

    async def attach(self, session_id: str | None, conn: ClientConnection) -> SessionRuntime:
        async with self._lock:
            row = self._resolve(session_id)
            if self.live is not None:
                await self._detach(self.live, error="another connection took over")
            runtime = self.runtimes.get(row.id)
            if runtime is None:
                runtime = await self._new_runtime(row)
                self.runtimes[row.id] = runtime
            runtime.conn = conn
            sink = ProtocolSink(conn, self.store, runtime)
            runtime.pipeline = self.pipeline_factory(
                self.stt_factory(),
                self.tts_factory(),
                runtime.engine,
                sink,
                on_dispatch=self._dispatcher(runtime),
            )
            runtime.run_task = asyncio.create_task(self._run_pipeline(runtime.pipeline))
            self.live = runtime
            self.store.touch(row.id)
            log(logger, "session_attached", session_id=row.id)
            await self._flush_pending(runtime)
            return runtime

    async def detach(self, runtime: SessionRuntime) -> None:
        async with self._lock:
            await self._detach(runtime)

    async def shutdown(self) -> None:
        async with self._lock:
            for runtime in self.runtimes.values():
                await self._detach(runtime)
                with contextlib.suppress(Exception):
                    await runtime.execution.stop()
                if runtime.pump_task is not None:
                    runtime.pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await runtime.pump_task

    def _resolve(self, session_id: str | None) -> SessionRow:
        if session_id is not None:
            row = self.store.get_session(session_id)
            if row is None:
                raise UnknownSession(session_id)
            return row
        return self.store.most_recent_session() or self.store.create_session()

    async def _new_runtime(self, row: SessionRow) -> SessionRuntime:
        engine = self.engine_factory()
        if row.messages:
            engine.load_messages(row.messages)
        runtime = SessionRuntime(row.id, engine, self.execution_factory())
        if row.execution_session_id:
            runtime.execution_session_id = row.execution_session_id
            try:
                await runtime.execution.resume(row.execution_session_id)
                runtime.execution_started = True
                self._start_pump(runtime)
            except Exception:
                logger.exception("execution resume failed")
        return runtime

    async def _detach(self, runtime: SessionRuntime, error: str | None = None) -> None:
        """Socket-level detach: close the pipeline, persist, keep the runtime alive."""
        if self.live is runtime:
            self.live = None
        pipeline, runtime.pipeline = runtime.pipeline, None
        conn, runtime.conn = runtime.conn, None
        run_task, runtime.run_task = runtime.run_task, None
        if pipeline is not None:
            with contextlib.suppress(Exception):
                await pipeline.close()
        if run_task is not None:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run_task
        persist_messages(self.store, runtime)
        if conn is not None and error is not None:
            await conn.close_with_error(error)
        log(logger, "session_detached", session_id=runtime.session_id, takeover=bool(error))

    def _dispatcher(self, runtime: SessionRuntime) -> OnDispatch:
        async def on_dispatch(directive: str) -> None:
            if runtime.execution_started:
                await runtime.execution.send(directive)
                return
            session_id = await runtime.execution.start(directive)
            runtime.execution_started = True
            runtime.execution_session_id = session_id
            self.store.set_execution_session(runtime.session_id, session_id)
            self._start_pump(runtime)
            log(logger, "execution_started", session_id=runtime.session_id, execution=session_id)

        return on_dispatch

    def _start_pump(self, runtime: SessionRuntime) -> None:
        if runtime.pump_task is None or runtime.pump_task.done():
            runtime.pump_task = asyncio.create_task(self._pump_events(runtime))

    async def _pump_events(self, runtime: SessionRuntime) -> None:
        try:
            async for event in runtime.execution.events():
                await self._deliver(runtime, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event pump failed")  # adapter contract says never raises

    async def _deliver(self, runtime: SessionRuntime, event: StatusEvent) -> None:
        log(logger, "execution_event", session_id=runtime.session_id, event_type=event.type)
        if runtime.pipeline is None or runtime.conn is None:
            runtime.pending_events.append(event)
            return
        await runtime.pipeline.on_events([event])
        await runtime.conn.send_message(protocol.Event(event=event))

    async def _flush_pending(self, runtime: SessionRuntime) -> None:
        if not runtime.pending_events or runtime.pipeline is None or runtime.conn is None:
            return
        events, runtime.pending_events = runtime.pending_events, []
        await runtime.pipeline.on_events(events)
        for event in events:
            await runtime.conn.send_message(protocol.Event(event=event))

    @staticmethod
    async def _run_pipeline(pipeline: AudioPipeline) -> None:
        try:
            await pipeline.run()
        except asyncio.CancelledError:
            raise
        except NotImplementedError:
            pass
        except Exception:
            logger.exception("pipeline run failed")
