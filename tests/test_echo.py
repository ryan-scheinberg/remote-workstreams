"""EchoGuard: transcripts of our own TTS get dropped; real speech passes.

Unit tests drive the guard with an injected clock; pipeline tests prove the
worst case end-to-end — the phone's mic hearing its own speaker must neither
barge in nor become a user turn.
"""

import asyncio
from types import SimpleNamespace

from test_pipeline import FakeConvo, FakeTTS, build, chunk, wait_for

from remote_workstreams.audio.echo import EchoGuard
from remote_workstreams.audio.state import PipelineState

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
    # Short interjections pass — they never open exactly like the reply.
    assert not g.is_echo("wait stop that")
    assert not g.is_echo("no the build")
    assert not g.is_echo("")
    assert not g.is_echo("   ")


def test_clipped_opening_is_echo_but_short_interjections_pass() -> None:
    """The clipping loop: barge-in clips playback at the reply's opening, so the
    echo endpoints as a short verbatim prefix ("sure easy test") — under the old
    len<5 blanket pass it committed as phantom user input on every long reply."""
    g, _ = guard()
    g.start_utterance()
    g.note_sentence("Sure, easy test. Just hit the plus workstream button.")
    g.note_audio(ONE_SECOND)
    # Growing interims of the echo — every prefix glimpse is caught.
    assert g.is_echo("sure")
    assert g.is_echo("sure easy")
    assert g.is_echo("Sure, easy test")
    # Verbatim words but not the opening: a human quoting mid-reply passes.
    assert not g.is_echo("plus workstream button")
    # Opens with our exact first word and stays similar: treated as a garbled
    # echo now (mishearing bit us live); a different first word stays human.
    assert g.is_echo("sure the test")
    assert not g.is_echo("so easy test")


def test_homophone_garble_is_echo() -> None:
    """The second live failure: Deepgram misheard the replayed reply — "Yep,
    Sonnet 5" came back as "yep it's on at five" and committed as a phantom
    turn ("Got it, confirmed."). No verbatim run survives a garble; character
    similarity of the digit-normalized text does."""
    g, _ = guard()
    g.start_utterance()
    g.note_sentence("Yep, Sonnet 5 — the current one, not four.")
    g.note_audio(4 * ONE_SECOND)
    assert g.is_echo("yep it's")  # the growing interim must not barge in
    assert g.is_echo("yep it's on at five")  # the endpointed phantom
    # Real speech about the same topic still passes.
    assert not g.is_echo("stop")
    assert not g.is_echo("what model are you running right now")


def test_garble_that_outgrows_the_reply_is_still_echo() -> None:
    g, clock = guard()
    g.start_utterance()
    g.note_sentence("I'm Sonnet 5.")  # "5" is spoken (and misheard) as words
    g.note_audio(ONE_SECOND)
    clock.t += 1.2
    assert g.is_echo("i'm connet 5 and five")  # the live incident's garbled interim
    assert g.is_echo("i'm sonnet five")  # digit/word mismatch alone


def test_tail_capture_is_echo_only_near_playback_end() -> None:
    """The third live failure: the mic caught just the reply's last word
    ("five" after "I'm Sonnet 5.") and it committed as a turn. An exact word
    suffix is echo — but only once those words have actually played, so a
    mid-playback "stop" can never be eaten by this rule."""
    g, clock = guard()
    g.start_utterance()
    g.note_sentence("The current one, not four — treat it as a stop.")
    g.note_audio(4 * ONE_SECOND)
    assert not g.is_echo("stop")  # mid-playback: the tail hasn't played yet
    clock.t += 3.5  # inside the final second of playback
    assert g.is_echo("stop")  # now it's the speaker, not the user
    assert g.is_echo("a stop")
    assert not g.is_echo("wait stop")  # not our tail — real speech


def test_short_reply_echo() -> None:
    g, clock = guard()
    g.start_utterance()
    g.note_sentence("Sounds good.")
    g.note_audio(ONE_SECOND)
    assert g.is_echo("sounds good")  # the phone playing us back
    # Similar enough to be a garble; within the window an eaten word costs one
    # repeat, a leaked echo costs a whole prompt-and-speak loop.
    assert g.is_echo("sounds bad")
    # Extends our words well beyond what played: a real reply, out of scope.
    assert not g.is_echo("sounds good let's move on")
    assert not g.is_echo("wait stop")
    assert not g.is_echo("good")  # mid-playback the tail hasn't played
    clock.t += 0.8
    assert g.is_echo("good")  # tail capture at the end of playback


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


async def test_short_echo_interims_neither_barge_in_nor_become_a_turn() -> None:
    """End-to-end replay of the clipping loop: Deepgram's first interims of an
    echo are 1-3 words; they must not barge in, and their endpointed final must
    not commit as user input."""
    tts = FakeTTS(hold_first=True)
    convo = FakeConvo(replies=[["Sure, easy test. Just hit the plus workstream button."]])
    pipeline, stt, tts, convo, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("kick off the test", is_final=True, speech_final=True))
    await wait_for(lambda: PipelineState.SPEAKING in sink.states and sink.audio_chunks)

    stt.push(chunk("sure"))  # the phone hearing our own opening words
    stt.push(chunk("sure easy"))
    stt.push(chunk("sure easy test", is_final=True))
    await asyncio.sleep(0.1)
    assert PipelineState.INTERRUPTED not in sink.states  # playback never clipped
    assert not any(role == "user" for role, _, _ in sink.transcripts)

    stt.push(chunk("", is_final=True, speech_final=True))  # the echo endpoints
    await asyncio.sleep(0.1)
    assert convo.turns == ["kick off the test"]  # no phantom user turn

    await pipeline.close()
    await asyncio.wait_for(task, 2)


async def test_homophone_echo_neither_barges_in_nor_becomes_a_turn() -> None:
    """End-to-end replay of the Sonnet-5 incident: the misheard echo of the
    reply must not clip playback, and its endpointed final must not commit."""
    tts = FakeTTS(hold_first=True)
    convo = FakeConvo(replies=[["Yep, Sonnet 5 — the current one, not four."]])
    pipeline, stt, tts, convo, sink = build(convo=convo, tts=tts)
    task = asyncio.create_task(pipeline.run())

    stt.push(chunk("what model is this", is_final=True, speech_final=True))
    await wait_for(lambda: PipelineState.SPEAKING in sink.states and sink.audio_chunks)

    stt.push(chunk("yep it's"))  # the phone mishearing our own TTS
    stt.push(chunk("yep it's on at five", is_final=True))
    await asyncio.sleep(0.1)
    assert PipelineState.INTERRUPTED not in sink.states  # playback never clipped
    assert not any(role == "user" for role, _, _ in sink.transcripts)

    stt.push(chunk("", is_final=True, speech_final=True))  # the echo endpoints
    await asyncio.sleep(0.1)
    assert convo.turns == ["what model is this"]  # no "Got it, confirmed." turn

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
