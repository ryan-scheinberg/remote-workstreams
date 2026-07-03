"""AudioPipeline drives one live audio session: mic PCM in → STT → engine turn →
sentence-chunked TTS → PCM out, with barge-in killing TTS the instant user speech
is detected during SPEAKING.

The public surface (constructor, run/feed/text/on_events/set_muted/close, AudioSink)
is frozen — the server and local frontend code against it.

Turn/interruption policy:
- Barge-in signal: any non-empty transcript chunk (interim or final) while SPEAKING.
  Client-side echoCancellation keeps the assistant's own voice out of the mic.
- On barge-in the in-flight turn task is cancelled and engine.turn()'s generator is
  closed. The engine owns its message list: we accept whatever reply text the engine
  recorded when its generator closed; the pipeline never appends text itself, so
  nothing is ever appended twice.
- A dispatch directive from an interrupted turn is drained via take_dispatch() and
  dropped: the user cut the reply off before its request for execution work reached
  their ears.
- User speech that endpoints while a turn is still THINKING supersedes it: the
  in-flight turn is cancelled and a fresh turn runs with the new text.
- Proactive-worthy events (completed/needs_approval) arriving while muted or mid-turn
  set a pending flag; the proactive turn runs on unmute or when the pipeline next
  returns to LISTENING.

Latency instrumentation: every turn logs one JSON line on the "voicecode.latency"
logger — endpoint decision, final transcript, engine first chunk, first TTS audio
byte, plus derived endpoint→first-audio ms. The ≤1.0s p50 target is measured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.state import PipelineState, StateMachine
from voicecode.engine.conversation import ConversationEngine
from voicecode.events import StatusEvent

logger = logging.getLogger(__name__)
latency_log = logging.getLogger("voicecode.latency")

_PROACTIVE_TYPES = ("completed", "needs_approval")


@dataclass
class TurnTimings:
    """Per-turn latency probe. Timestamps are time.time() seconds (TranscriptChunk.ts
    uses the same clock)."""

    kind: Literal["voice", "text", "proactive"] = "voice"
    endpoint_ts: float | None = None  # STT endpoint decision (speech_final chunk)
    transcript_ts: float | None = None  # final transcript committed / input received
    engine_first_chunk_ts: float | None = None
    first_audio_ts: float | None = None
    end_ts: float | None = None
    interrupted: bool = False

    def log_line(self) -> str:
        data = asdict(self)
        start = self.endpoint_ts or self.transcript_ts
        if start is not None and self.first_audio_ts is not None:
            data["endpoint_to_first_audio_ms"] = round((self.first_audio_ts - start) * 1000, 1)
        return json.dumps(data)


async def _aclose(gen: object) -> None:
    """Close an async generator deterministically (task cancellation alone leaves
    finalization to the GC)."""
    aclose = getattr(gen, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):
            await aclose()


class AudioSink(Protocol):
    """Where pipeline output goes — a WebSocket connection or the local speaker."""

    async def state(self, state: PipelineState) -> None: ...

    async def transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None: ...

    async def audio(self, pcm: bytes) -> None: ...

    async def speech_end(self) -> None: ...


class AudioPipeline:
    def __init__(
        self,
        stt: STTAdapter,
        tts: TTSAdapter,
        engine: ConversationEngine,
        sink: AudioSink,
        on_dispatch: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.stt = stt
        self.tts = tts
        self.engine = engine
        self.sink = sink
        self.on_dispatch = on_dispatch  # after each turn, engine.take_dispatch() routes here
        self.muted = False
        self._sm = StateMachine()
        self._mic: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finals: list[str] = []  # finalized spans of the in-progress user utterance
        self._turn_task: asyncio.Task[None] | None = None
        self._unmute_task: asyncio.Task[None] | None = None
        self._pending_proactive = False
        self._closed = False

    @property
    def state(self) -> PipelineState:
        return self._sm.state

    # ---- frozen public surface ----

    async def run(self) -> None:
        """Main loop; consumes audio fed via feed() until close()."""
        try:
            async for chunk in self.stt.stream(self._mic_audio()):
                await self._handle_chunk(chunk)
        finally:
            await self._abort_turn()

    async def feed(self, pcm: bytes) -> None:
        """Mic audio from the client (protocol.MIC_FORMAT)."""
        if not self._closed:
            await self._mic.put(pcm)

    async def text(self, text: str) -> None:
        """Typed input; skips STT, flows through the same turn machinery."""
        text = text.strip()
        if not text:
            return
        timings = TurnTimings(kind="text", transcript_ts=time.time())
        await self.sink.transcript("user", text, final=True)
        await self._start_turn(lambda: self.engine.turn(text), timings)

    async def on_events(self, events: Sequence[StatusEvent]) -> None:
        """Bridge input: inject into the engine; completed/needs_approval trigger
        proactive speech when the user is silent and not muted."""
        events = list(events)
        if not events:
            return
        self.engine.inject_events(events)
        if not any(event.type in _PROACTIVE_TYPES for event in events):
            return
        if not self.muted and self._sm.state is PipelineState.LISTENING:
            await self._start_proactive()
        else:
            self._pending_proactive = True

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if (
            not muted
            and self._pending_proactive
            and self._sm.state is PipelineState.LISTENING
            and not self._closed
        ):
            self._unmute_task = asyncio.create_task(self._start_proactive())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._unmute_task is not None and not self._unmute_task.done():
            self._unmute_task.cancel()
        await self._abort_turn()
        await self._mic.put(None)  # ends _mic_audio → STT stream ends → run() returns

    # ---- internals ----

    async def _mic_audio(self) -> AsyncIterator[bytes]:
        while True:
            pcm = await self._mic.get()
            if pcm is None:
                return
            yield pcm

    async def _set_state(self, new: PipelineState) -> None:
        self._sm.to(new)
        await self.sink.state(new)

    async def _handle_chunk(self, chunk: TranscriptChunk) -> None:
        text = chunk.text.strip()
        if text and self._sm.state is PipelineState.SPEAKING:
            await self._barge_in()
        if text:
            if chunk.is_final:
                self._finals.append(text)
                display = " ".join(self._finals)
            else:
                display = " ".join([*self._finals, text])
            if not chunk.speech_final:
                await self.sink.transcript("user", display, final=False)
        if chunk.speech_final:
            await self._endpoint(chunk)

    async def _barge_in(self) -> None:
        await self._abort_turn()  # stops audio immediately, cancels TTS
        await self._set_state(PipelineState.INTERRUPTED)

    async def _endpoint(self, chunk: TranscriptChunk) -> None:
        text = " ".join(self._finals)
        self._finals.clear()
        if not text:
            if self._sm.state is PipelineState.INTERRUPTED:
                await self._set_state(PipelineState.LISTENING)  # barge-in was noise
            return
        timings = TurnTimings(kind="voice", endpoint_ts=chunk.ts, transcript_ts=time.time())
        await self.sink.transcript("user", text, final=True)
        await self._start_turn(lambda: self.engine.turn(text), timings)

    async def _start_turn(
        self, factory: Callable[[], AsyncIterator[str]], timings: TurnTimings
    ) -> None:
        if self._sm.state is PipelineState.SPEAKING:
            await self._barge_in()  # typed input during speech behaves like barge-in
        if self._sm.state is PipelineState.THINKING:
            await self._abort_turn()  # new user speech supersedes the in-flight turn
        elif (
            self._turn_task is not None
            and not self._turn_task.done()
            and self._turn_task is not asyncio.current_task()
        ):
            await self._turn_task  # back in LISTENING but still routing dispatch
        if self._sm.state is not PipelineState.THINKING:
            await self._set_state(PipelineState.THINKING)
        self._turn_task = asyncio.create_task(self._speak_turn(factory(), timings))

    async def _start_proactive(self) -> None:
        self._pending_proactive = False
        timings = TurnTimings(kind="proactive", transcript_ts=time.time())
        await self._start_turn(lambda: self.engine.proactive_turn(), timings)

    async def _speak_turn(self, chunks: AsyncIterator[str], timings: TurnTimings) -> None:
        spoken: list[str] = []
        tts_stream: AsyncIterator[bytes] | None = None
        try:
            async for sentence in chunks:
                if timings.engine_first_chunk_ts is None:
                    timings.engine_first_chunk_ts = time.time()
                sentence = sentence.strip()
                if not sentence:
                    continue
                await self.sink.transcript("assistant", sentence, final=False)
                tts_stream = self.tts.synthesize(sentence)
                async for pcm in tts_stream:
                    if timings.first_audio_ts is None:
                        timings.first_audio_ts = time.time()
                        await self._set_state(PipelineState.SPEAKING)
                    await self.sink.audio(pcm)
                tts_stream = None
                spoken.append(sentence)
            if spoken:
                await self.sink.transcript("assistant", " ".join(spoken), final=True)
            if timings.first_audio_ts is not None:
                await self.sink.speech_end()
            await self._set_state(PipelineState.LISTENING)
            await self._route_dispatch()
        except asyncio.CancelledError:
            timings.interrupted = True
            await _aclose(tts_stream)
            await _aclose(chunks)
            raise
        except Exception:
            logger.exception("turn failed")
            await _aclose(tts_stream)
            await _aclose(chunks)
            if self._sm.state is not PipelineState.LISTENING:
                await self._set_state(PipelineState.LISTENING)
        finally:
            timings.end_ts = time.time()
            latency_log.info(timings.log_line())
        if (
            self._pending_proactive
            and not self.muted
            and self._sm.state is PipelineState.LISTENING
        ):
            await self._start_proactive()

    async def _route_dispatch(self) -> None:
        directive = self.engine.take_dispatch()
        if directive and self.on_dispatch is not None:
            await self.on_dispatch(directive)

    async def _abort_turn(self) -> None:
        task = self._turn_task
        self._turn_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.tts.cancel()
        self.engine.take_dispatch()  # drop any unrouted directive from the aborted turn
