"""The barge-in seam across the two REAL units: ConversationEngine driven through
AudioPipeline, mocking only the Anthropic SDK boundary (FakeClient) and STT/TTS.
The unit suites only ever pair real-engine+fake-pipeline or fake-engine+real-pipeline.

Adopted from the first-pass QA probes (2026-07-02).
"""

import asyncio

from test_conversation import FakeClient
from test_pipeline import FakeTTS, RecordingSink, ScriptedSTT, chunk, wait_for

from voicecode.audio.pipeline import AudioPipeline
from voicecode.audio.state import PipelineState
from voicecode.engine.conversation import ConversationEngine
from voicecode.events import Completed

LISTENING = PipelineState.LISTENING
SPEAKING = PipelineState.SPEAKING
INTERRUPTED = PipelineState.INTERRUPTED


def build(replies, tts=None, sink=None, on_dispatch=None):
    client = FakeClient(replies)
    engine = ConversationEngine(client)
    stt = ScriptedSTT()
    tts = tts or FakeTTS()
    sink = sink or RecordingSink()
    pipeline = AudioPipeline(stt, tts, engine, sink, on_dispatch=on_dispatch)
    return pipeline, stt, tts, engine, sink, client


async def test_barge_in_mid_stream_then_clean_next_turn():
    """Interrupt while the engine generator is mid-stream: no dispatch exists to
    route, the message list stays sane, and the next turn's API payload is legal."""
    dispatches: list[str] = []

    async def on_dispatch(d: str) -> None:
        dispatches.append(d)

    replies = [
        "First sentence goes right here. Second sentence would follow it."
        "<dispatch>stale directive from the interrupted turn</dispatch>",
        "Okay, stopping that now.<dispatch>the second turn directive</dispatch>",
    ]
    tts = FakeTTS(hold_first=True)
    pipeline, stt, tts, engine, sink, client = build(replies, tts=tts, on_dispatch=on_dispatch)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("start the demo", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states and sink.audio_chunks)

    stt.push(chunk("wait"))  # barge-in mid-utterance (TTS parked on first sentence)
    await wait_for(lambda: INTERRUPTED in sink.states)
    await asyncio.sleep(0.05)  # let cancellation/finalization settle

    # the real engine records nothing on GeneratorExit — no reply, no dispatch
    assert dispatches == []
    assert engine.take_dispatch() is None
    assert [m["role"] for m in engine.messages] == ["user"]

    stt.push(chunk("wait stop that", is_final=True, speech_final=True))
    await wait_for(
        lambda: any(t == ("assistant", "Okay, stopping that now.", True) for t in sink.transcripts)
        and sink.states[-1] is LISTENING
    )
    await wait_for(lambda: dispatches == ["the second turn directive"])

    assert [m["role"] for m in engine.messages] == ["user", "user", "assistant"]
    # payload actually sent for turn 2: consecutive user messages (legal on the
    # Messages API), no dangling assistant
    sent = client.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "user"]
    assert all(isinstance(m["content"], str) and m["content"].strip() for m in sent)

    # no partial/duplicated assistant text anywhere
    finals = [t for t in sink.transcripts if t[0] == "assistant" and t[2]]
    assert finals == [("assistant", "Okay, stopping that now.", True)]

    resumed = ConversationEngine(FakeClient([]))
    resumed.load_messages(engine.export_messages())
    assert resumed.messages == engine.messages

    await pipeline.close()
    await asyncio.wait_for(task, 2)


class HoldFinalSink(RecordingSink):
    """Parks on the FINAL assistant transcript so a barge-in can land in the window
    after the engine generator exhausted (reply + dispatch recorded) but before
    dispatch routing."""

    def __init__(self) -> None:
        super().__init__()
        self.hold = asyncio.Event()
        self.parked = asyncio.Event()

    async def transcript(self, role, text, final) -> None:
        await super().transcript(role, text, final)
        if role == "assistant" and final:
            self.parked.set()
            await self.hold.wait()


async def test_barge_in_after_stream_end_still_routes_dispatch():
    """Once the reply fully streams, history keeps the <dispatch> tag — so the
    handoff must route even when barge-in lands before routing would have run,
    or the model would remember delegating work that never started."""
    dispatches: list[str] = []

    async def on_dispatch(d: str) -> None:
        dispatches.append(d)

    replies = ["Sure, kicking that off now.<dispatch>rename the loader</dispatch>", "Fine."]
    sink = HoldFinalSink()
    pipeline, stt, tts, engine, sink, client = build(replies, sink=sink, on_dispatch=on_dispatch)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("do the thing", is_final=True, speech_final=True))
    await asyncio.wait_for(sink.parked.wait(), 2)  # reply fully streamed + recorded

    stt.push(chunk("no wait"))  # barge-in in the post-stream window (state SPEAKING)
    await wait_for(lambda: INTERRUPTED in sink.states)
    sink.hold.set()
    await asyncio.sleep(0.05)

    assert dispatches == ["rename the loader"]  # routed despite the interrupt
    assert engine.take_dispatch() is None
    # history retains the raw reply with the tag — and the handoff really happened
    assert engine.messages[-1]["role"] == "assistant"
    assert "<dispatch>" in engine.messages[-1]["content"]

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_mute_during_speaking_queued_proactive_fires_once():
    replies = [
        "Answering your question here now.",
        "The build you asked about just finished.",
    ]
    tts = FakeTTS(hold_first=True)
    pipeline, stt, tts, engine, sink, client = build(replies, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("question", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)

    pipeline.set_muted(True)
    await pipeline.on_events([Completed(summary="Build finished.")])
    tts.release()
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    await asyncio.sleep(0.05)
    assert len(client.calls) == 1  # muted: no proactive yet

    pipeline.set_muted(False)
    await wait_for(
        lambda: any(
            t == ("assistant", "The build you asked about just finished.", True)
            for t in sink.transcripts
        )
    )
    await asyncio.sleep(0.05)
    assert len(client.calls) == 2  # exactly one proactive API call — not two
    finals = [t for t in sink.transcripts if t[0] == "assistant" and t[2]]
    assert len(finals) == 2

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_double_unmute_speaks_proactive_exactly_once():
    replies = [
        "The deploy you kicked off completed.",
        "It really did complete just now.",
    ]
    pipeline, stt, tts, engine, sink, client = build(replies)
    task = asyncio.create_task(pipeline.run())

    pipeline.set_muted(True)
    await pipeline.on_events([Completed(summary="Deploy completed.")])
    await asyncio.sleep(0.02)

    pipeline.set_muted(False)
    pipeline.set_muted(False)  # rapid double-unmute (laggy client / double tap)
    await asyncio.sleep(0.3)

    finals = [t for t in sink.transcripts if t[0] == "assistant" and t[2]]
    assert finals == [("assistant", "The deploy you kicked off completed.", True)]
    assert len(client.calls) == 1

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_whitespace_endpoint_commits_nothing():
    pipeline, stt, tts, engine, sink, client = build(["Should never be requested."])
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("   ", is_final=True, speech_final=True))
    stt.push(chunk("\n\t", is_final=True, speech_final=True))
    await asyncio.sleep(0.05)
    assert client.calls == []
    assert engine.messages == []
    assert sink.states == []
    await pipeline.text("   \n ")  # typed whitespace likewise
    await asyncio.sleep(0.05)
    assert client.calls == []
    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_binary_flood_during_turn_no_exception():
    replies = ["A reply that keeps the turn busy while frames flood in."]
    tts = FakeTTS(hold_first=True)
    pipeline, stt, tts, engine, sink, client = build(replies, tts=tts)
    task = asyncio.create_task(pipeline.run())
    stt.push(chunk("go", is_final=True, speech_final=True))
    await wait_for(lambda: SPEAKING in sink.states)
    for _ in range(500):
        await pipeline.feed(b"\x00\x01" * 160)
    tts.release()
    await wait_for(lambda: sink.states[-1:] == [LISTENING])
    await wait_for(lambda: len(stt.fed) == 500)
    await pipeline.close()
    await asyncio.wait_for(task, 2)
