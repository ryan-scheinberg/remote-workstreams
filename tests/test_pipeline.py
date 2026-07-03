"""AudioPipeline behavior: turn flow, barge-in, typed input, proactive events,
dispatch routing, latency logging. All fakes — no live providers, no audio devices."""

from __future__ import annotations

import asyncio
import json
import logging

from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.pipeline import AudioPipeline
from voicecode.audio.state import PipelineState
from voicecode.events import Completed, Progress

LISTENING = PipelineState.LISTENING
THINKING = PipelineState.THINKING
SPEAKING = PipelineState.SPEAKING
INTERRUPTED = PipelineState.INTERRUPTED


def chunk(text: str, *, is_final: bool = False, speech_final: bool = False) -> TranscriptChunk:
    return TranscriptChunk(text=text, is_final=is_final, speech_final=speech_final)


async def wait_for(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class ScriptedSTT(STTAdapter):
    """Test-controlled STT: yields chunks pushed via push(). Like the real adapter,
    the transcript stream ends once the mic iterator ends (pipeline.close())."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TranscriptChunk | None] = asyncio.Queue()
        self.fed: list[bytes] = []

    def push(self, *chunks: TranscriptChunk) -> None:
        for c in chunks:
            self._queue.put_nowait(c)

    async def stream(self, audio):
        consume = asyncio.create_task(self._consume(audio))
        try:
            while True:
                c = await self._queue.get()
                if c is None:
                    return
                yield c
        finally:
            consume.cancel()

    async def _consume(self, audio) -> None:
        async for pcm in audio:
            self.fed.append(pcm)
        self._queue.put_nowait(None)  # mic ended → transcript stream ends


class FakeTTS(TTSAdapter):
    """Two PCM chunks per sentence. hold_first parks the first synthesize call after
    its first chunk (mid-utterance), so tests can barge in deterministically."""

    def __init__(self, hold_first: bool = False) -> None:
        self.synthesized: list[str] = []
        self.cancels = 0
        self._hold = asyncio.Event() if hold_first else None

    def release(self) -> None:
        assert self._hold is not None
        self._hold.set()

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield b"\x01\x02" * 160
        if self._hold is not None and len(self.synthesized) == 1:
            await self._hold.wait()
        yield b"\x03\x04" * 160

    async def cancel(self) -> None:
        self.cancels += 1


class FakeEngine:
    """replies[i] is the sentence-chunk list for turn i (last one repeats)."""

    def __init__(
        self,
        replies: list[list[str]] | None = None,
        dispatch_after_turn: str | None = None,
        dispatch_early: str | None = None,
        first_turn_gate: asyncio.Event | None = None,
    ) -> None:
        self.replies = replies if replies is not None else [["Sure thing."]]
        self.dispatch_after_turn = dispatch_after_turn
        self.dispatch_early = dispatch_early
        self.first_turn_gate = first_turn_gate
        self.turns: list[str] = []
        self.injected: list = []
        self.dispatch: str | None = None
        self.closed_mid_turn = False
        self.proactive_replies: list[list[str]] = []
        self.proactive_calls = 0

    def inject_events(self, events) -> None:
        self.injected.extend(events)

    async def turn(self, user_text: str):
        self.turns.append(user_text)
        if self.dispatch_early:
            self.dispatch = self.dispatch_early
        index = min(len(self.turns) - 1, len(self.replies) - 1)
        try:
            if self.first_turn_gate is not None and len(self.turns) == 1:
                await self.first_turn_gate.wait()
            for part in self.replies[index]:
                yield part
        except (GeneratorExit, asyncio.CancelledError):
            self.closed_mid_turn = True
            raise
        if self.dispatch_after_turn:
            self.dispatch = self.dispatch_after_turn

    async def proactive_turn(self):
        self.proactive_calls += 1
        if self.proactive_replies:
            for part in self.proactive_replies.pop(0):
                yield part

    def take_dispatch(self) -> str | None:
        directive, self.dispatch = self.dispatch, None
        return directive


class RecordingSink:
    def __init__(self) -> None:
        self.states: list[PipelineState] = []
        self.transcripts: list[tuple[str, str, bool]] = []
        self.audio_chunks: list[bytes] = []
        self.speech_ends = 0

    async def state(self, state: PipelineState) -> None:
        self.states.append(state)

    async def transcript(self, role, text, final) -> None:
        self.transcripts.append((role, text, final))

    async def audio(self, pcm: bytes) -> None:
        self.audio_chunks.append(pcm)

    async def speech_end(self) -> None:
        self.speech_ends += 1


def build(engine=None, tts=None, on_dispatch=None):
    stt = ScriptedSTT()
    tts = tts or FakeTTS()
    engine = engine or FakeEngine()
    sink = RecordingSink()
    pipeline = AudioPipeline(stt, tts, engine, sink, on_dispatch=on_dispatch)
    return pipeline, stt, tts, engine, sink


async def test_full_voice_turn() -> None:
    pipeline, stt, tts, engine, sink = build()
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("hello"))
    stt.push(chunk("hello there", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert sink.states == [THINKING, SPEAKING, LISTENING]
    assert engine.turns == ["hello there"]
    assert ("user", "hello", False) in sink.transcripts  # interim forwarded
    assert ("user", "hello there", True) in sink.transcripts
    assert ("assistant", "Sure thing.", False) in sink.transcripts
    assert ("assistant", "Sure thing.", True) in sink.transcripts
    assert len(sink.audio_chunks) == 2
    assert sink.speech_ends == 1
    assert tts.synthesized == ["Sure thing."]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_feed_reaches_stt() -> None:
    pipeline, stt, _, _, _ = build()
    task = asyncio.create_task(pipeline.run())
    await pipeline.feed(b"\x00\x01")
    await wait_for(lambda: stt.fed == [b"\x00\x01"])
    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_barge_in() -> None:
    engine = FakeEngine(replies=[["First sentence.", "Second sentence."], ["Okay."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=engine, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("start the demo", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states and sink.audio_chunks)

    audio_before = len(sink.audio_chunks)
    stt.push(chunk("wait"))  # non-empty interim during SPEAKING = barge-in
    await wait_for(lambda: INTERRUPTED in sink.states)

    assert tts.cancels >= 1
    assert engine.closed_mid_turn  # in-flight engine generator closed cleanly
    await asyncio.sleep(0.02)
    assert len(sink.audio_chunks) == audio_before  # audio stopped immediately

    stt.push(chunk("wait stop that", is_final=True, speech_final=True))
    await wait_for(lambda: sink.speech_ends == 1 and sink.states[-1] is LISTENING)

    assert engine.turns == ["start the demo", "wait stop that"]
    assert sink.states == [THINKING, SPEAKING, INTERRUPTED, THINKING, SPEAKING, LISTENING]
    # only the completed turn's reply committed as final assistant text — never the
    # interrupted one, and never twice
    finals = [t for t in sink.transcripts if t[0] == "assistant" and t[2]]
    assert finals == [("assistant", "Okay.", True)]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_barge_in_noise_returns_to_listening() -> None:
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, engine, sink = build(engine=FakeEngine(replies=[["Reply."]]), tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("hi there", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    stt.push(chunk("uh"))  # interim blip
    await wait_for(lambda: INTERRUPTED in sink.states)
    stt.push(chunk("", is_final=True, speech_final=True))  # endpoints to nothing
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert engine.turns == ["hi there"]  # no second turn from noise
    assert sink.states == [THINKING, SPEAKING, INTERRUPTED, LISTENING]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_speech_during_thinking_supersedes_turn() -> None:
    gate = asyncio.Event()  # never set: first turn parks before yielding
    engine = FakeEngine(replies=[["Old reply."], ["New reply."]], first_turn_gate=gate)
    pipeline, stt, tts, _, sink = build(engine=engine)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("first question", is_final=True, speech_final=True))
    await wait_for(lambda: THINKING in sink.states)
    stt.push(chunk("second question", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert engine.turns == ["first question", "second question"]
    assert engine.closed_mid_turn
    assert tts.synthesized == ["New reply."]
    assert sink.states == [THINKING, SPEAKING, LISTENING]  # no INTERRUPTED mid-THINKING

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_typed_input_runs_turn_without_stt() -> None:
    pipeline, _, _, engine, sink = build()
    await pipeline.text("  deploy it  ")
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    assert engine.turns == ["deploy it"]
    assert ("user", "deploy it", True) in sink.transcripts
    assert sink.speech_ends == 1
    await pipeline.close()


async def test_typed_input_interrupts_speech() -> None:
    engine = FakeEngine(replies=[["Long reply."], ["Second."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=engine, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("talk to me", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    await pipeline.text("never mind")
    await wait_for(lambda: sink.speech_ends == 1 and sink.states[-1] is LISTENING)

    assert INTERRUPTED in sink.states
    assert engine.turns == ["talk to me", "never mind"]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_proactive_on_completed_event() -> None:
    engine = FakeEngine()
    engine.proactive_replies = [["The build finished."]]
    pipeline, _, _, _, sink = build(engine=engine)
    event = Completed(summary="The build finished.")

    await pipeline.on_events([event])
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert engine.injected == [event]  # always injected
    assert engine.proactive_calls == 1
    assert sink.speech_ends == 1
    assert ("assistant", "The build finished.", True) in sink.transcripts
    await pipeline.close()


async def test_progress_event_injected_but_silent() -> None:
    pipeline, _, _, engine, sink = build()
    await pipeline.on_events([Progress(summary="Still compiling.")])
    await asyncio.sleep(0.02)
    assert len(engine.injected) == 1
    assert engine.proactive_calls == 0
    assert sink.states == []
    await pipeline.close()


async def test_muted_queues_proactive_until_unmute() -> None:
    engine = FakeEngine()
    engine.proactive_replies = [["Done now."]]
    pipeline, _, _, _, sink = build(engine=engine)

    pipeline.set_muted(True)
    await pipeline.on_events([Completed(summary="Done now.")])
    await asyncio.sleep(0.02)
    assert len(engine.injected) == 1  # injected even while muted
    assert engine.proactive_calls == 0
    assert sink.states == []

    pipeline.set_muted(False)
    await wait_for(lambda: sink.speech_ends == 1)
    assert engine.proactive_calls == 1
    await pipeline.close()


async def test_proactive_with_nothing_to_say_returns_to_listening() -> None:
    pipeline, _, _, engine, sink = build()  # proactive_replies empty → yields nothing
    await pipeline.on_events([Completed(summary="Quiet finish.")])
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    assert sink.states == [THINKING, LISTENING]
    assert sink.audio_chunks == []
    assert sink.speech_ends == 0
    await pipeline.close()


async def test_event_during_turn_speaks_after_turn_ends() -> None:
    engine = FakeEngine(replies=[["Answering."]])
    engine.proactive_replies = [["Build done."]]
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=engine, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("question", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    await pipeline.on_events([Completed(summary="Build done.")])  # mid-turn: queued
    assert engine.proactive_calls == 0

    tts.release()  # let the current turn finish
    await wait_for(lambda: engine.proactive_calls == 1 and sink.speech_ends == 2)

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_dispatch_routed_after_turn() -> None:
    dispatches: list[str] = []

    async def on_dispatch(directive: str) -> None:
        dispatches.append(directive)

    engine = FakeEngine(dispatch_after_turn="fix the failing tests")
    pipeline, _, _, _, _ = build(engine=engine, on_dispatch=on_dispatch)
    await pipeline.text("please fix the tests")
    await wait_for(lambda: dispatches == ["fix the failing tests"])
    await pipeline.close()


async def test_dispatch_dropped_on_barge_in() -> None:
    dispatches: list[str] = []

    async def on_dispatch(directive: str) -> None:
        dispatches.append(directive)

    engine = FakeEngine(replies=[["A.", "B."], ["Fine."]], dispatch_early="stale directive")
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=engine, tts=tts, on_dispatch=on_dispatch)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("do the thing", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    stt.push(chunk("no wait"))  # barge-in before the turn completes
    await wait_for(lambda: INTERRUPTED in sink.states)

    assert dispatches == []
    assert engine.dispatch is None  # drained, not left to leak into the next turn

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_noise_endpoint_ignored() -> None:
    pipeline, stt, _, engine, sink = build()
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("", is_final=True, speech_final=True))
    await asyncio.sleep(0.02)
    assert engine.turns == []
    assert sink.states == []
    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_close_cancels_inflight_turn() -> None:
    engine = FakeEngine(replies=[["Held reply."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=engine, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("hello", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    await pipeline.close()
    await asyncio.wait_for(task, 1)
    assert tts.cancels >= 1
    assert engine.closed_mid_turn


async def test_latency_logged_per_turn(caplog) -> None:
    caplog.set_level(logging.INFO, logger="voicecode.latency")
    pipeline, stt, _, _, sink = build()
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("time me", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    await pipeline.close()
    await asyncio.wait_for(task, 1)

    records = [r for r in caplog.records if r.name == "voicecode.latency"]
    assert len(records) == 1
    data = json.loads(records[0].message)
    assert data["kind"] == "voice"
    assert data["endpoint_ts"] is not None
    assert data["transcript_ts"] is not None
    assert data["engine_first_chunk_ts"] is not None
    assert data["first_audio_ts"] is not None
    assert data["endpoint_to_first_audio_ms"] >= 0
    assert data["interrupted"] is False


async def test_interrupted_turn_logged(caplog) -> None:
    caplog.set_level(logging.INFO, logger="voicecode.latency")
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(engine=FakeEngine(replies=[["Held."]]), tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("hello", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    stt.push(chunk("stop"))
    await wait_for(lambda: INTERRUPTED in sink.states)

    records = [r for r in caplog.records if r.name == "voicecode.latency"]
    assert len(records) == 1
    assert json.loads(records[0].message)["interrupted"] is True

    await pipeline.close()
    await asyncio.wait_for(task, 1)
