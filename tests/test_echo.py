"""EchoGuard: transcripts of our own TTS get dropped; real speech passes.

Unit tests drive the guard with an injected clock; pipeline tests prove the
worst case end-to-end — the phone's mic hearing its own speaker must neither
barge in nor become a user turn.
"""

import asyncio
from types import SimpleNamespace

from test_pipeline import FakeConvo, FakeTTS, build, chunk, wait_for

from voicecode.audio.echo import EchoGuard
from voicecode.audio.state import PipelineState

ONE_SECOND = 48000  # bytes of 24 kHz s16le mono


def guard(start: float = 1000.0) -> tuple[EchoGuard, SimpleNamespace]:
    clock = SimpleNamespace(t=start)
    return EchoGuard(now=lambda: clock.t), clock


def test_echo_matches_verbatim_run_not_topical_overlap() -> None:
    g, _ = guard()
    g.start_utterance()
    g.note_sentence("The build is green and every deploy just went out clean.")
    g.note_audio(ONE_SECOND)
    # A long verbatim run of what we said = echo.
    assert g.is_echo("the build is green and every deploy")
    assert g.is_echo("The BUILD is green and every deploy just went")
    # A human reply reusing the topic's words, but not quoting us verbatim, passes.
    assert not g.is_echo("did the build go green or not")
    assert not g.is_echo("so every deploy is done then great")
    # Short utterances — barge-ins included — always pass (never suppressed).
    assert not g.is_echo("wait stop that")
    assert not g.is_echo("no the build")
    assert not g.is_echo("")
    assert not g.is_echo("   ")


def test_no_audio_sent_means_no_echo() -> None:
    g, _ = guard()
    g.start_utterance()
    g.note_sentence("Nothing at all has been played yet.")
    assert not g.is_echo("nothing at all has been played")


def test_window_tracks_playback_duration() -> None:
    g, clock = guard()
    g.start_utterance()
    g.note_sentence("The deploys are all live now.")
    g.note_audio(ONE_SECOND)  # playback ends at t0 + 1.0
    clock.t += 2.0  # inside 1.0 + 1.5 margin
    assert g.is_echo("the deploys are all live")
    clock.t += 1.0  # t0 + 3.0: past the window
    assert not g.is_echo("the deploys are all live")


def test_cut_off_shrinks_the_window_to_what_played() -> None:
    g, clock = guard()
    g.start_utterance()
    g.note_sentence("A very long reply that got interrupted early.")
    g.note_audio(10 * ONE_SECOND)
    clock.t += 1.0
    g.cut_off()  # client flushed after ~1s of playback
    clock.t += 1.0  # t0 + 2.0 < 1.0 + 1.5
    assert g.is_echo("a very long reply that")
    clock.t += 0.7  # t0 + 2.7 > 1.0 + 1.5
    assert not g.is_echo("a very long reply that")


def test_start_utterance_resets() -> None:
    g, _ = guard()
    g.start_utterance()
    g.note_sentence("Old reply text.")
    g.note_audio(ONE_SECOND)
    g.start_utterance()
    assert not g.is_echo("old reply text")


async def test_echo_interim_while_speaking_does_not_barge_in() -> None:
    tts = FakeTTS(hold_first=True)
    convo = FakeConvo(replies=[["The build is green and every deploy is live."], ["Fine."]])
    pipeline, stt, tts, convo, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("status update please now", is_final=True, speech_final=True))
    await wait_for(lambda: PipelineState.SPEAKING in sink.states and sink.audio_chunks)

    stt.push(chunk("the build is green and every deploy"))  # the phone hearing our own TTS
    await asyncio.sleep(0.1)
    assert PipelineState.INTERRUPTED not in sink.states
    assert not any("build" in text for _, text, _ in sink.transcripts)

    stt.push(chunk("wait stop"))  # real user speech still barges in
    await wait_for(lambda: PipelineState.INTERRUPTED in sink.states)

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_post_stream_echo_does_not_become_a_turn() -> None:
    convo = FakeConvo(replies=[["Every deploy just went out clean and green."]])
    pipeline, stt, tts, convo, sink = build(convo=convo)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("go", is_final=True, speech_final=True))
    await wait_for(lambda: sink.speech_ends == 1 and sink.states[-1] is PipelineState.LISTENING)

    # Server-side the utterance is over, but the phone is still playing it out.
    stt.push(chunk("every deploy just went out clean", is_final=True, speech_final=True))
    await asyncio.sleep(0.1)
    assert convo.turns == ["go"]  # echo never committed as user speech

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_mute_mid_utterance_commits_the_pending_turn() -> None:
    pipeline, stt, tts, convo, sink = build()
    task = asyncio.create_task(pipeline.run())

    # Finalized words but no endpoint: the user muted before Deepgram's silence window.
    stt.push(chunk("did it really work", is_final=True))
    await wait_for(lambda: any(t == ("user", "did it really work", False) for t in sink.transcripts))

    pipeline.set_muted(True)
    await wait_for(lambda: convo.turns == ["did it really work"])

    await pipeline.close()
    await asyncio.wait_for(task, 2)
