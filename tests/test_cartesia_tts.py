"""CartesiaTTS against a fake SDK client — no live API."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from remote_workstreams.adapters import cartesia_tts
from remote_workstreams.adapters.cartesia_tts import DEFAULT_VOICE_ID, CartesiaTTS


def chunk_event(pcm: bytes):
    return SimpleNamespace(type="chunk", audio=pcm)


DONE = SimpleNamespace(type="done", audio=None)
ERROR = SimpleNamespace(type="error", audio=None, error="boom")


class FakeStream:
    """Mimics the SDK AsyncStream: async-iterable; close() aborts the HTTP stream
    (further iteration raises, like a closed httpx response)."""

    def __init__(self, events, hold_after: int | None = None) -> None:
        self.events = events
        self.hold_after = hold_after
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for i, event in enumerate(self.events):
            if self.closed.is_set():
                raise RuntimeError("stream closed")
            yield event
            if self.hold_after is not None and i + 1 == self.hold_after:
                await self.closed.wait()
                raise RuntimeError("stream closed")


def install(monkeypatch, stream):
    holder: dict = {}

    async def generate_sse(**kwargs):
        holder["kwargs"] = kwargs
        return stream

    def make_client(*, api_key: str):
        holder["api_key"] = api_key
        return SimpleNamespace(tts=SimpleNamespace(generate_sse=generate_sse))

    monkeypatch.setattr(cartesia_tts, "AsyncCartesia", make_client)
    return holder


async def test_synthesize_streams_pcm(monkeypatch) -> None:
    stream = FakeStream([chunk_event(b"aaaa"), chunk_event(b""), chunk_event(b"bbbb"), DONE])
    holder = install(monkeypatch, stream)

    tts = CartesiaTTS("ct-key")
    got = [pcm async for pcm in tts.synthesize("Hello there.")]

    assert got == [b"aaaa", b"bbbb"]  # empty chunks skipped
    assert holder["api_key"] == "ct-key"
    kwargs = holder["kwargs"]
    assert kwargs["transcript"] == "Hello there."
    assert kwargs["model_id"] == "sonic-3"
    assert kwargs["voice"] == {"mode": "id", "id": DEFAULT_VOICE_ID}
    assert kwargs["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24000,
    }
    assert stream.closed.is_set()  # stream released after completion


async def test_cancel_aborts_in_flight_and_is_idempotent(monkeypatch) -> None:
    stream = FakeStream([chunk_event(b"aaaa"), chunk_event(b"bbbb")], hold_after=1)
    install(monkeypatch, stream)
    tts = CartesiaTTS("ct-key")

    got: list[bytes] = []

    async def consume() -> None:
        async for pcm in tts.synthesize("Hello."):
            got.append(pcm)

    task = asyncio.create_task(consume())
    async with asyncio.timeout(1):
        while not got:
            await asyncio.sleep(0.005)

    await tts.cancel()
    await asyncio.wait_for(task, 1)  # ends cleanly — no exception from teardown noise
    assert got == [b"aaaa"]
    assert stream.closed.is_set()

    await tts.cancel()  # idempotent: nothing in flight
    await tts.cancel()


async def test_error_event_raises(monkeypatch) -> None:
    install(monkeypatch, FakeStream([ERROR]))
    tts = CartesiaTTS("ct-key")
    with pytest.raises(RuntimeError, match="cartesia synthesis failed"):
        async for _ in tts.synthesize("Hello."):
            pass


async def test_voice_and_model_overridable(monkeypatch) -> None:
    holder = install(monkeypatch, FakeStream([DONE]))
    tts = CartesiaTTS("ct-key", voice_id="custom-voice", model_id="sonic-3.5")
    [_ async for _ in tts.synthesize("Hi.")]
    assert holder["kwargs"]["voice"] == {"mode": "id", "id": "custom-voice"}
    assert holder["kwargs"]["model_id"] == "sonic-3.5"
