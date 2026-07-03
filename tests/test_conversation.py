"""ConversationEngine tests. The Anthropic client is faked at the SDK boundary:
the fake mimics `client.messages.stream(...)` returning an async context manager
whose `text_stream` yields small deltas (so tags and sentence boundaries split
across deltas, like the real stream)."""

import copy
from typing import Any

from voicecode.engine import ConversationEngine
from voicecode.engine.prompt import PROACTIVE_NOTE, SYSTEM_PROMPT
from voicecode.events import Completed, Finding, NeedsApproval, Progress, TaskStarted

DELTA_SIZE = 7


class FakeStream:
    def __init__(self, client: "FakeClient", deltas: list[str]) -> None:
        self._client = client
        self._deltas = deltas
        self.text_stream = self._gen()

    async def _gen(self):
        for delta in self._deltas:
            self._client.deltas_served += 1
            yield delta


class FakeStreamManager:
    def __init__(self, client: "FakeClient", deltas: list[str]) -> None:
        self._client = client
        self._deltas = deltas

    async def __aenter__(self) -> FakeStream:
        return FakeStream(self._client, self._deltas)

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeClient:
    """Duck-typed AsyncAnthropic: `client.messages.stream(**kw)` records and replays."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.deltas_served = 0
        self.total_deltas = 0
        self.messages = self

    def stream(self, **kwargs: Any) -> FakeStreamManager:
        self.calls.append(copy.deepcopy(kwargs))  # engine mutates messages after the call
        text = self._replies.pop(0)
        deltas = [text[i : i + DELTA_SIZE] for i in range(0, len(text), DELTA_SIZE)]
        self.total_deltas += len(deltas)
        return FakeStreamManager(self, deltas)


async def collect(agen) -> list[str]:
    return [chunk async for chunk in agen]


async def test_turn_yields_sentence_chunks_and_appends_history():
    client = FakeClient(["First sentence goes here. Second sentence lands after it."])
    engine = ConversationEngine(client)
    chunks = await collect(engine.turn("hello there"))
    assert chunks == [
        "First sentence goes here.",
        "Second sentence lands after it.",
    ]
    assert engine.messages == [
        {"role": "user", "content": "hello there"},
        {
            "role": "assistant",
            "content": "First sentence goes here. Second sentence lands after it.",
        },
    ]


async def test_first_chunk_yields_before_stream_is_consumed():
    reply = "This is the first full sentence right here. " + (
        "And then the reply keeps rambling on for a good while longer. " * 4
    )
    client = FakeClient([reply])
    engine = ConversationEngine(client)
    gen = engine.turn("hi")
    first = await anext(gen)
    assert first == "This is the first full sentence right here."
    assert client.deltas_served < client.total_deltas  # latency: didn't wait for the end
    await collect(gen)


async def test_events_render_first_then_user_words_then_drain():
    client = FakeClient(["Got it, thanks for asking about that."] * 2)
    engine = ConversationEngine(client)
    engine.inject_events(
        [
            Finding(summary="The bug is in token refresh."),
            NeedsApproval(summary="Wants to run the tests.", gate_id="g1", tool_name="Bash"),
        ]
    )
    await collect(engine.turn("what's new?"))
    prompt = client.calls[0]["messages"][0]["content"]
    assert prompt.startswith("<system-reminder>")
    assert prompt.endswith("what's new?")
    assert "- [finding] The bug is in token refresh." in prompt
    assert "- [needs_approval] Wants to run the tests. (tool: Bash)" in prompt

    await collect(engine.turn("and now?"))
    second_prompt = client.calls[1]["messages"][-1]["content"]
    assert second_prompt == "and now?"  # queue drained by the first turn


async def test_system_prompt_frozen_and_cache_marked():
    client = FakeClient(["Okay, sounds good to me."] * 2)
    engine = ConversationEngine(client)
    await collect(engine.turn("one"))
    await collect(engine.turn("two"))
    first, second = (call["system"] for call in client.calls)
    assert first == second  # byte-stable across turns — the cache prefix survives
    assert first[-1]["cache_control"] == {"type": "ephemeral"}
    assert first[-1]["text"] == SYSTEM_PROMPT
    assert client.calls[0]["model"] == "claude-haiku-4-5"


async def test_dispatch_stripped_from_speech_taken_once_kept_in_history():
    client = FakeClient(
        ["Sure, kicking that off now.<dispatch>Rename the config loader to settings</dispatch>"]
    )
    engine = ConversationEngine(client)
    chunks = await collect(engine.turn("rename the config loader"))
    spoken = " ".join(chunks)
    assert "dispatch" not in spoken
    assert spoken == "Sure, kicking that off now."
    assert engine.take_dispatch() == "Rename the config loader to settings"
    assert engine.take_dispatch() is None  # returns once, then clears
    # raw reply (tag included) stays in history so later turns remember the handoff
    assert "<dispatch>" in engine.messages[-1]["content"]


async def test_no_dispatch_on_plain_turn():
    client = FakeClient(["Just saying hello back to you."])
    engine = ConversationEngine(client)
    await collect(engine.turn("hi"))
    assert engine.take_dispatch() is None


async def test_proactive_silent_without_noteworthy_events():
    client = FakeClient([])
    engine = ConversationEngine(client)
    assert await collect(engine.proactive_turn()) == []
    assert client.calls == []  # no API call at all

    engine.inject_events([Progress(summary="Editing the token refresh logic.")])
    assert await collect(engine.proactive_turn()) == []
    assert client.calls == []


async def test_progress_events_ride_along_in_next_real_turn():
    client = FakeClient(["Still moving along on that."])
    engine = ConversationEngine(client)
    engine.inject_events([Progress(summary="Editing the token refresh logic.")])
    await collect(engine.proactive_turn())  # no-op, keeps the queue
    await collect(engine.turn("how's it going?"))
    prompt = client.calls[0]["messages"][0]["content"]
    assert "- [progress] Editing the token refresh logic." in prompt


async def test_proactive_speaks_on_completed():
    client = FakeClient(
        [
            "The auth refactor just finished, and every test passes.",
            "You're welcome, happy to help.",
        ]
    )
    engine = ConversationEngine(client)
    engine.inject_events(
        [
            TaskStarted(summary="Started the auth refactor."),
            Completed(summary="Finished the auth refactor."),
        ]
    )
    chunks = await collect(engine.proactive_turn())
    assert chunks  # it spoke
    prompt = client.calls[0]["messages"][0]["content"]
    assert "- [completed] Finished the auth refactor." in prompt
    assert prompt.endswith(PROACTIVE_NOTE)
    # queue fully drained — nothing left for the next turn
    await collect(engine.turn("thanks"))
    assert "<system-reminder>" not in client.calls[1]["messages"][-1]["content"]


async def test_proactive_speaks_on_needs_approval():
    client = FakeClient(["Claude wants to run the test suite — should it go ahead?"])
    engine = ConversationEngine(client)
    engine.inject_events(
        [NeedsApproval(summary="Wants to run the test suite.", gate_id="g1", tool_name="Bash")]
    )
    chunks = await collect(engine.proactive_turn())
    assert chunks
    assert len(client.calls) == 1


async def test_empty_reply_appends_no_assistant_message():
    client = FakeClient([""])
    engine = ConversationEngine(client)
    assert await collect(engine.turn("hello?")) == []
    assert engine.messages == [{"role": "user", "content": "hello?"}]


async def test_export_and_load_roundtrip():
    client = FakeClient(["Here's a reply for the export test."])
    engine = ConversationEngine(client)
    await collect(engine.turn("persist me"))
    exported = engine.export_messages()
    assert exported == engine.messages
    exported[0]["content"] = "mutated"
    assert engine.messages[0]["content"] == "persist me"  # snapshot is a copy

    resumed = ConversationEngine(FakeClient([]))
    resumed.load_messages(engine.export_messages())
    assert resumed.messages == engine.messages
