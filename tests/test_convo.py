"""ConvoBridge behavior against a real growing JSONL file and a fake substrate:
fan-out, history, turn sentence streaming, supersession, early close, shutdown."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from remote_workstreams.convo import ConvoBridge
from remote_workstreams.substrate import CCSession, SessionSpec
from remote_workstreams.transcript import AssistantText, Entry, ToolActivity, TurnEnd, UserText

TS = "2026-07-03T10:00:00.000Z"


def user_line(text: str) -> str:
    return json.dumps({"type": "user", "timestamp": TS, "message": {"role": "user", "content": text}})


def assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "timestamp": TS, "message": {"content": [{"type": "text", "text": text}]}}
    )


def tool_line() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": TS,
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]},
        }
    )


def turn_end_line() -> str:
    return json.dumps({"type": "system", "subtype": "turn_duration", "timestamp": TS, "durationMs": 42})


def append(path: Path, *lines: str) -> None:
    with path.open("a") as f:
        for line in lines:
            f.write(line + "\n")


class FakeSubstrate:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.slashes: list[str] = []

    async def send(self, session: CCSession, text: str) -> None:
        self.sent.append(text)

    async def slash(self, session: CCSession, command: str) -> None:
        self.slashes.append(command)


def build(tmp_path: Path) -> tuple[ConvoBridge, FakeSubstrate, Path]:
    transcript = tmp_path / "convo.jsonl"
    transcript.touch()
    session = CCSession(
        session_id="s-1",
        window="voice:convo",
        transcript=transcript,
        spec=SessionSpec(name="convo", model="fable", effort="low", display_name="convo"),
    )
    substrate = FakeSubstrate()
    return ConvoBridge(substrate, session, poll_interval=0.01), substrate, transcript


async def wait_for(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def collect(stream, into: list) -> None:
    async for item in stream:
        into.append(item)


async def test_fan_out_to_two_subscribers(tmp_path: Path) -> None:
    bridge, _, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    seen_a: list[Entry] = []
    seen_b: list[Entry] = []
    task_a = asyncio.create_task(collect(bridge.subscribe(), seen_a))
    task_b = asyncio.create_task(collect(bridge.subscribe(), seen_b))

    append(transcript, user_line("hello"), assistant_line("Hi."), turn_end_line())
    await wait_for(lambda: len(seen_a) == 3 and len(seen_b) == 3)

    expected = [UserText(text="hello", ts=TS), AssistantText(text="Hi.", ts=TS), TurnEnd(ts=TS)]
    assert seen_a == expected
    assert seen_b == expected

    await bridge.close()
    await asyncio.wait_for(asyncio.gather(run, task_a, task_b), 1)


async def test_history_returns_last_n(tmp_path: Path) -> None:
    bridge, _, transcript = build(tmp_path)
    append(transcript, *[user_line(f"message {i}") for i in range(5)])
    entries = bridge.history(limit=3)
    assert entries == [UserText(text=f"message {i}", ts=TS) for i in (2, 3, 4)]
    assert len(bridge.history()) == 5


async def test_turn_streams_sentences_until_turn_end(tmp_path: Path) -> None:
    bridge, substrate, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    sentences: list[str] = []
    turn = asyncio.create_task(collect(bridge.turn("what's next?"), sentences))
    await wait_for(lambda: substrate.sent == ["what's next?"])

    append(transcript, assistant_line("First sentence lands here. Then a second one follows."))
    await wait_for(lambda: len(sentences) == 2)
    append(transcript, assistant_line("A closing thought arrives."), turn_end_line())
    await asyncio.wait_for(turn, 1)  # TurnEnd ends the generator

    assert sentences == [
        "First sentence lands here.",
        "Then a second one follows.",
        "A closing thought arrives.",
    ]
    await bridge.close()
    await asyncio.wait_for(run, 1)


async def test_tool_activity_skipped_by_turn_but_fanned_out(tmp_path: Path) -> None:
    bridge, _, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    seen: list[Entry] = []
    sub = asyncio.create_task(collect(bridge.subscribe(), seen))
    sentences: list[str] = []
    turn = asyncio.create_task(collect(bridge.turn("check the tests"), sentences))

    append(transcript, tool_line(), assistant_line("Tests are green now, all of them."), turn_end_line())
    await asyncio.wait_for(turn, 1)

    assert sentences == ["Tests are green now, all of them."]
    await wait_for(lambda: len(seen) == 3)
    assert isinstance(seen[0], ToolActivity)

    await bridge.close()
    await asyncio.wait_for(asyncio.gather(run, sub), 1)


async def test_new_turn_detaches_old_stream(tmp_path: Path) -> None:
    """Mid-turn input queues in the session (Milestone-0), so the superseded turn's
    reply and TurnEnd land first; the new stream skips them and speaks only its own."""
    bridge, substrate, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    old_sentences: list[str] = []
    old_turn = asyncio.create_task(collect(bridge.turn("first ask"), old_sentences))
    await wait_for(lambda: substrate.sent == ["first ask"])

    new_sentences: list[str] = []
    new_turn = asyncio.create_task(collect(bridge.turn("second ask"), new_sentences))
    await asyncio.wait_for(old_turn, 1)  # old stream ends quietly

    append(transcript, assistant_line("Answering the first ask, superseded."), turn_end_line())
    append(transcript, assistant_line("Answering the second ask only."), turn_end_line())
    await asyncio.wait_for(new_turn, 1)

    assert substrate.sent == ["first ask", "second ask"]
    assert old_sentences == []
    assert new_sentences == ["Answering the second ask only."]

    await bridge.close()
    await asyncio.wait_for(run, 1)


async def test_turn_after_barge_in_skips_leftovers_of_interrupted_turn(tmp_path: Path) -> None:
    """Barge-in aborts the stream but the session keeps writing turn 1; the next
    turn must survive turn 1's leftover blocks and TurnEnd and speak only reply 2."""
    bridge, substrate, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())

    stream = bridge.turn("start the demo")
    append(transcript, assistant_line("A long reply that gets interrupted mid-speech."))
    assert await anext(stream) == "A long reply that gets interrupted mid-speech."
    await stream.aclose()  # barge-in

    sentences: list[str] = []
    turn = asyncio.create_task(collect(bridge.turn("wait stop that"), sentences))
    await wait_for(lambda: substrate.sent == ["start the demo", "wait stop that"])
    append(transcript, assistant_line("Leftover tail of the interrupted reply."), turn_end_line())
    append(transcript, assistant_line("Okay, switching gears."), turn_end_line())
    await asyncio.wait_for(turn, 1)

    assert sentences == ["Okay, switching gears."]
    await bridge.close()
    await asyncio.wait_for(run, 1)


async def test_early_turn_close_leaves_fan_out_intact(tmp_path: Path) -> None:
    bridge, _, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    seen: list[Entry] = []
    sub = asyncio.create_task(collect(bridge.subscribe(), seen))

    stream = bridge.turn("talk to me")
    append(transcript, assistant_line("A reply that gets barged into, sadly."))
    assert await anext(stream) == "A reply that gets barged into, sadly."
    await stream.aclose()  # barge-in

    append(transcript, assistant_line("The session kept writing anyway."), turn_end_line())
    await wait_for(lambda: len(seen) == 3)  # subscribers got every entry

    await bridge.close()
    await asyncio.wait_for(asyncio.gather(run, sub), 1)


async def test_reset_swaps_session_and_next_turn_speaks(tmp_path: Path) -> None:
    """Clear: the tail follows the fresh transcript, the in-flight turn stream ends
    quietly, and the next turn speaks its reply (no TurnEnd owed from the old file)."""
    bridge, substrate, transcript = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    seen: list[Entry] = []
    sub = asyncio.create_task(collect(bridge.subscribe(), seen))
    orphaned: list[str] = []
    old_turn = asyncio.create_task(collect(bridge.turn("still there?"), orphaned))
    await wait_for(lambda: substrate.sent == ["still there?"])

    fresh = tmp_path / "convo-fresh.jsonl"
    fresh.touch()
    bridge.reset(
        CCSession(
            session_id="s-2",
            window="voice:convo",
            transcript=fresh,
            spec=SessionSpec(name="convo", model="fable", effort="low", display_name="convo"),
        )
    )
    await asyncio.wait_for(old_turn, 1)
    assert orphaned == []

    sentences: list[str] = []
    turn = asyncio.create_task(collect(bridge.turn("hello again"), sentences))
    await wait_for(lambda: substrate.sent == ["still there?", "hello again"])
    append(transcript, assistant_line("A ghost from the killed session."))  # never read
    append(fresh, assistant_line("Hello from the fresh session."), turn_end_line())
    await asyncio.wait_for(turn, 1)

    assert sentences == ["Hello from the fresh session."]
    await wait_for(lambda: len(seen) == 2)  # fresh entries only, ghost dropped
    assert seen[0] == AssistantText(text="Hello from the fresh session.", ts=TS)
    assert bridge.history() == seen  # history reads the fresh transcript too

    await bridge.close()
    await asyncio.wait_for(asyncio.gather(run, sub), 1)


async def test_send_and_slash_reach_substrate(tmp_path: Path) -> None:
    bridge, substrate, _ = build(tmp_path)
    await bridge.send("just so you know")
    await bridge.slash("/compact")
    assert substrate.sent == ["just so you know"]
    assert substrate.slashes == ["/compact"]


async def test_close_stops_run(tmp_path: Path) -> None:
    bridge, _, _ = build(tmp_path)
    run = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.02)
    await bridge.close()
    await asyncio.wait_for(run, 1)
