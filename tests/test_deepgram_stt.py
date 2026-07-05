"""DeepgramSTT against a fake SDK transport — no live API, no sockets."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from remote_workstreams.adapters import deepgram_stt
from remote_workstreams.adapters.deepgram_stt import DeepgramSTT


def results(text: str, *, is_final: bool = False, speech_final: bool = False):
    return SimpleNamespace(
        type="Results",
        channel=SimpleNamespace(alternatives=[SimpleNamespace(transcript=text)]),
        is_final=is_final,
        speech_final=speech_final,
    )


UTTERANCE_END = SimpleNamespace(type="UtteranceEnd", last_word_end=1.9)
METADATA = SimpleNamespace(type="Metadata")


class FakeConnection:
    """Pumps scripted messages after the adapter sends CloseStream (i.e. after the
    mic iterator ended), mimicking Deepgram's flush-then-close behavior."""

    def __init__(self, messages) -> None:
        self.messages = messages
        self.media: list[bytes] = []
        self.close_stream = asyncio.Event()
        self.keepalives = 0

    async def send_media(self, data: bytes) -> None:
        self.media.append(data)

    async def send_close_stream(self, message=None) -> None:
        self.close_stream.set()

    async def send_keep_alive(self, message=None) -> None:
        self.keepalives += 1

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        await self.close_stream.wait()
        for message in self.messages:
            yield message


class FakeClient:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.connect_kwargs: dict | None = None

        @asynccontextmanager
        async def connect(**kwargs):
            self.connect_kwargs = kwargs
            yield self.connection

        self.listen = SimpleNamespace(v1=SimpleNamespace(connect=connect))


def install(monkeypatch, connection):
    client = FakeClient(connection)
    captured: dict = {}

    def make_client(*, api_key: str):
        captured["api_key"] = api_key
        return client

    monkeypatch.setattr(deepgram_stt, "AsyncDeepgramClient", make_client)
    return client, captured


async def test_streams_audio_and_maps_chunks(monkeypatch) -> None:
    connection = FakeConnection(
        [
            results(""),
            results("hello"),
            results("hello world", is_final=True, speech_final=True),
            UTTERANCE_END,
            METADATA,
        ]
    )
    client, captured = install(monkeypatch, connection)

    async def mic():
        yield b"aa"
        yield b"bb"

    chunks = [c async for c in DeepgramSTT("dg-key").stream(mic())]

    assert captured["api_key"] == "dg-key"
    assert connection.media == [b"aa", b"bb"]

    kwargs = client.connect_kwargs
    assert kwargs["model"] == "nova-3"
    assert kwargs["encoding"] == "linear16"
    assert kwargs["sample_rate"] == 16000
    assert kwargs["channels"] == 1
    assert kwargs["interim_results"] is True
    assert kwargs["endpointing"] == deepgram_stt.ENDPOINTING_MS
    assert kwargs["utterance_end_ms"] == deepgram_stt.UTTERANCE_END_MS

    shaped = [(c.text, c.is_final, c.speech_final) for c in chunks]
    assert shaped == [
        ("", False, False),
        ("hello", False, False),
        ("hello world", True, True),
        ("", True, True),  # UtteranceEnd fallback endpoint; Metadata ignored
    ]


async def test_keepalive_sent_while_mic_idle(monkeypatch) -> None:
    monkeypatch.setattr(deepgram_stt, "KEEPALIVE_INTERVAL", 0.01)

    class IdleConnection(FakeConnection):
        async def _iter(self):
            while self.keepalives < 2:
                await asyncio.sleep(0.005)
            return
            yield  # pragma: no cover — makes this a generator

    connection = IdleConnection([])
    install(monkeypatch, connection)

    async def silent_mic():
        await asyncio.Event().wait()  # never yields
        yield b""  # pragma: no cover

    chunks = [c async for c in DeepgramSTT("dg-key").stream(silent_mic())]
    assert chunks == []
    assert connection.keepalives >= 2
