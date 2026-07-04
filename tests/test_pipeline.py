"""AudioPipeline behavior: turn flow, barge-in, typed input, muting, latency logging.
All fakes — no live providers, no audio devices, no tmux."""

from __future__ import annotations

import asyncio
import json
import logging

from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.pipeline import AudioPipeline
from voicecode.audio.state import PipelineState

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


class FakeConvo:
    """ConvoPort fake: replies[i] is the sentence list for turn i (last one repeats).
    first_turn_gate parks the first turn before any sentence, like a slow session."""

    def __init__(
        self,
        replies: list[list[str]] | None = None,
        first_turn_gate: asyncio.Event | None = None,
    ) -> None:
        self.replies = replies if replies is not None else [["Sure thing."]]
        self.first_turn_gate = first_turn_gate
        self.turns: list[str] = []
        self.closed_mid_turn = False

    async def turn(self, text: str):
        self.turns.append(text)
        index = min(len(self.turns) - 1, len(self.replies) - 1)
        try:
            if self.first_turn_gate is not None and len(self.turns) == 1:
                await self.first_turn_gate.wait()
            for part in self.replies[index]:
                yield part
        except (GeneratorExit, asyncio.CancelledError):
            self.closed_mid_turn = True
            raise


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


def assert_interims_only(sink: RecordingSink) -> None:
    """The chat-sourcing rule: the sink only ever carries user STT interims."""
    assert all(role == "user" and not final for role, _, final in sink.transcripts)


def build(convo=None, tts=None):
    stt = ScriptedSTT()
    tts = tts or FakeTTS()
    convo = convo or FakeConvo()
    sink = RecordingSink()
    pipeline = AudioPipeline(stt, tts, convo, sink)
    return pipeline, stt, tts, convo, sink


async def test_full_voice_turn() -> None:
    pipeline, stt, tts, convo, sink = build()
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("hello"))
    stt.push(chunk("hello there", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert sink.states == [THINKING, SPEAKING, LISTENING]
    assert convo.turns == ["hello there"]
    assert sink.transcripts == [("user", "hello", False)]  # live caption only
    assert_interims_only(sink)
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


async def test_muted_feed_drops_frames() -> None:
    pipeline, stt, _, _, _ = build()
    task = asyncio.create_task(pipeline.run())
    pipeline.set_muted(True)
    await pipeline.feed(b"\x00\x01")
    await asyncio.sleep(0.02)
    assert stt.fed == []
    pipeline.set_muted(False)
    await pipeline.feed(b"\x02\x03")
    await wait_for(lambda: stt.fed == [b"\x02\x03"])
    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_binary_flood_is_safe() -> None:
    pipeline, stt, _, convo, sink = build()
    task = asyncio.create_task(pipeline.run())
    for _ in range(500):
        await pipeline.feed(b"\xff" * 640)
    stt.push(chunk("still works", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    assert len(stt.fed) == 500
    assert convo.turns == ["still works"]
    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_barge_in() -> None:
    convo = FakeConvo(replies=[["First sentence.", "Second sentence."], ["Okay."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("start the demo", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states and sink.audio_chunks)

    audio_before = len(sink.audio_chunks)
    stt.push(chunk("wait"))  # non-empty interim during SPEAKING = barge-in
    await wait_for(lambda: INTERRUPTED in sink.states)

    assert tts.cancels >= 1
    assert convo.closed_mid_turn  # the sentence stream was abandoned cleanly
    await asyncio.sleep(0.02)
    assert len(sink.audio_chunks) == audio_before  # audio stopped immediately

    stt.push(chunk("wait stop that", is_final=True, speech_final=True))
    await wait_for(lambda: sink.speech_ends == 1 and sink.states[-1] is LISTENING)

    assert convo.turns == ["start the demo", "wait stop that"]
    assert sink.states == [THINKING, SPEAKING, INTERRUPTED, THINKING, SPEAKING, LISTENING]
    assert_interims_only(sink)

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_barge_in_noise_returns_to_listening() -> None:
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, convo, sink = build(convo=FakeConvo(replies=[["Reply."]]), tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("hi there", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    stt.push(chunk("uh"))  # interim blip
    await wait_for(lambda: INTERRUPTED in sink.states)
    stt.push(chunk("", is_final=True, speech_final=True))  # endpoints to nothing
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert convo.turns == ["hi there"]  # no second turn from noise
    assert sink.states == [THINKING, SPEAKING, INTERRUPTED, LISTENING]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_speech_during_thinking_supersedes_turn() -> None:
    gate = asyncio.Event()  # never set: first turn parks before yielding
    convo = FakeConvo(replies=[["Old reply."], ["New reply."]], first_turn_gate=gate)
    pipeline, stt, tts, _, sink = build(convo=convo)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("first question", is_final=True, speech_final=True))
    await wait_for(lambda: THINKING in sink.states)
    stt.push(chunk("second question", is_final=True, speech_final=True))
    await wait_for(lambda: sink.states[-1:] == [LISTENING])

    assert convo.turns == ["first question", "second question"]
    assert convo.closed_mid_turn
    assert tts.synthesized == ["New reply."]
    assert sink.states == [THINKING, SPEAKING, LISTENING]  # no INTERRUPTED mid-THINKING

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_typed_input_runs_turn_without_stt() -> None:
    pipeline, _, _, convo, sink = build()
    await pipeline.text("  deploy it  ")
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    assert convo.turns == ["deploy it"]
    assert sink.transcripts == []  # final user text reaches chat via the transcript
    assert sink.speech_ends == 1
    await pipeline.close()


async def test_typed_input_interrupts_speech() -> None:
    convo = FakeConvo(replies=[["Long reply."], ["Second."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("talk to me", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    await pipeline.text("never mind")
    await wait_for(lambda: sink.speech_ends == 1 and sink.states[-1] is LISTENING)

    assert INTERRUPTED in sink.states
    assert convo.turns == ["talk to me", "never mind"]

    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_whitespace_endpoint_commits_nothing() -> None:
    pipeline, stt, _, convo, sink = build()
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("   ", is_final=True, speech_final=True))
    await asyncio.sleep(0.02)
    assert convo.turns == []
    assert sink.states == []
    await pipeline.close()
    await asyncio.wait_for(task, 1)


async def test_close_cancels_inflight_turn() -> None:
    convo = FakeConvo(replies=[["Held reply."]])
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("hello", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    await pipeline.close()
    await asyncio.wait_for(task, 1)
    assert tts.cancels >= 1
    assert convo.closed_mid_turn


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
    assert data["first_sentence_ts"] is not None
    assert data["first_audio_ts"] is not None
    assert data["endpoint_to_first_audio_ms"] >= 0
    assert data["interrupted"] is False


async def test_interrupted_turn_logged(caplog) -> None:
    caplog.set_level(logging.INFO, logger="voicecode.latency")
    tts = FakeTTS(hold_first=True)
    pipeline, stt, _, _, sink = build(convo=FakeConvo(replies=[["Held."]]), tts=tts)
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
