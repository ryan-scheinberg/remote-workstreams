"""AudioPipeline drives one live audio session: mic PCM in → STT → convo turn →
sentence-chunked TTS → PCM out, with barge-in killing TTS the instant user speech
is detected during SPEAKING.

The conversation is a real Claude Code session behind ConvoPort: turn() streams
TTS-ready sentences parsed from the session's transcript and ends when the
transcript's turn_duration marker (TurnEnd) arrives.

Turn/interruption policy:
- Barge-in signal: any non-empty transcript chunk (interim or final) while SPEAKING.
  Client-side echoCancellation keeps the assistant's own voice out of the mic.
- Barge-in silences TTS and abandons the sentence stream, but the session keeps
  writing — the full reply still lands in chat from the transcript.
- User speech that endpoints while a turn is still THINKING supersedes it: the
  in-flight turn is cancelled and a fresh turn runs with the new text.
- sink.transcript carries ONLY user STT interims (final=False, the live caption).
  Final text of both roles reaches the UI from ConvoBridge entries via the server.

Latency instrumentation: every turn logs one JSON line on the "voicecode.latency"
logger — endpoint decision, final transcript, first sentence, first TTS audio byte,
plus derived endpoint→first-audio ms.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.echo import EchoGuard
from voicecode.audio.state import PipelineState, StateMachine

logger = logging.getLogger(__name__)
latency_log = logging.getLogger("voicecode.latency")


@dataclass
class TurnTimings:
    """Per-turn latency probe. Timestamps are time.time() seconds (TranscriptChunk.ts
    uses the same clock)."""

    kind: Literal["voice", "text"] = "voice"
    endpoint_ts: float | None = None  # STT endpoint decision (speech_final chunk)
    transcript_ts: float | None = None  # final transcript committed / input received
    first_sentence_ts: float | None = None
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


class ConvoPort(Protocol):
    """The pipeline's view of the conversation session: text in, sentences out."""

    def turn(self, text: str) -> AsyncIterator[str]: ...


class AudioSink(Protocol):
    """Where pipeline output goes — a WebSocket connection or the local speaker.

    transcript() only ever carries user STT interims (final=False)."""

    async def state(self, state: PipelineState) -> None: ...

    async def transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None: ...

    async def audio(self, pcm: bytes) -> None: ...

    async def speech_end(self) -> None: ...


class AudioPipeline:
    def __init__(
        self,
        stt: STTAdapter,
        tts: TTSAdapter,
        convo: ConvoPort,
        sink: AudioSink,
    ) -> None:
        self.stt = stt
        self.tts = tts
        self.convo = convo
        self.sink = sink
        self.muted = False
        self._echo = EchoGuard()
        self._sm = StateMachine()
        self._mic: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finals: list[str] = []  # finalized spans of the in-progress user utterance
        self._turn_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def state(self) -> PipelineState:
        return self._sm.state

    # ---- public surface ----

    async def run(self) -> None:
        """Main loop; consumes audio fed via feed() until close()."""
        try:
            async for chunk in self.stt.stream(self._mic_audio()):
                await self._handle_chunk(chunk)
        finally:
            await self._abort_turn()

    async def feed(self, pcm: bytes) -> None:
        """Mic audio from the client (protocol.MIC_FORMAT); dropped while muted."""
        if not self._closed and not self.muted:
            await self._mic.put(pcm)

    async def text(self, text: str) -> None:
        """Typed input; skips STT, flows through the same turn machinery."""
        text = text.strip()
        if not text:
            return
        timings = TurnTimings(kind="text", transcript_ts=time.time())
        await self._start_turn(lambda: self.convo.turn(text), timings)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
        if text and self._echo.is_echo(text):
            text = ""  # the phone replaying our own TTS, not the user
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
        await self._start_turn(lambda: self.convo.turn(text), timings)

    async def _start_turn(
        self, factory: Callable[[], AsyncIterator[str]], timings: TurnTimings
    ) -> None:
        if self._sm.state is PipelineState.SPEAKING:
            await self._barge_in()  # typed input during speech behaves like barge-in
        if self._sm.state is PipelineState.THINKING:
            await self._abort_turn()  # new user input supersedes the in-flight turn
        elif self._turn_task is not None and not self._turn_task.done():
            await self._turn_task  # back in LISTENING, finishing its last steps
        if self._sm.state is not PipelineState.THINKING:
            await self._set_state(PipelineState.THINKING)
        self._turn_task = asyncio.create_task(self._speak_turn(factory(), timings))

    async def _speak_turn(self, chunks: AsyncIterator[str], timings: TurnTimings) -> None:
        tts_stream: AsyncIterator[bytes] | None = None
        self._echo.start_utterance()
        try:
            async for sentence in chunks:
                if timings.first_sentence_ts is None:
                    timings.first_sentence_ts = time.time()
                sentence = sentence.strip()
                if not sentence:
                    continue
                self._echo.note_sentence(sentence)
                tts_stream = self.tts.synthesize(sentence)
                async for pcm in tts_stream:
                    if timings.first_audio_ts is None:
                        timings.first_audio_ts = time.time()
                        await self._set_state(PipelineState.SPEAKING)
                    self._echo.note_audio(len(pcm))
                    await self.sink.audio(pcm)
                tts_stream = None
            if timings.first_audio_ts is not None:
                await self.sink.speech_end()
            await self._set_state(PipelineState.LISTENING)
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

    async def _abort_turn(self) -> None:
        task = self._turn_task
        self._turn_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.tts.cancel()
        self._echo.cut_off()  # client flushes its buffer too; unplayed audio can't echo
