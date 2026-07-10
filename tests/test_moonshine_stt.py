from __future__ import annotations

from types import SimpleNamespace

from remote_workstreams.adapters.moonshine_stt import MoonshineSTT


class FakeTranscriber:
    def __init__(self) -> None:
        self.listener = None
        self.audio: list[tuple[list[float], int]] = []

    def add_listener(self, listener) -> None:
        self.listener = listener

    def start(self) -> None:
        pass

    def add_audio(self, samples, sample_rate: int) -> None:
        self.audio.append((samples, sample_rate))
        line = SimpleNamespace(text="hello")
        self.listener.on_line_text_changed(SimpleNamespace(line=line))

    def stop(self) -> None:
        self.listener.on_line_completed(SimpleNamespace(line=SimpleNamespace(text="hello world")))


async def test_stream_maps_partial_and_completed_line() -> None:
    fake = FakeTranscriber()

    async def audio():
        yield (0).to_bytes(2, "little", signed=True) + (32767).to_bytes(2, "little", signed=True)

    chunks = [c async for c in MoonshineSTT(transcriber_factory=lambda: fake).stream(audio())]

    assert [(c.text, c.is_final, c.speech_final) for c in chunks] == [
        ("hello", False, False),
        ("hello world", True, True),
    ]
    assert fake.audio[0][1] == 16000
    assert fake.audio[0][0] == [0.0, 32767 / 32768.0]


async def test_stream_deduplicates_repeated_partials() -> None:
    fake = FakeTranscriber()

    def add_audio(samples, sample_rate):
        fake.audio.append((samples, sample_rate))
        line = SimpleNamespace(text="same")
        fake.listener.on_line_text_changed(SimpleNamespace(line=line))
        fake.listener.on_line_text_changed(SimpleNamespace(line=line))

    fake.add_audio = add_audio

    async def audio():
        yield b"\x00\x00"

    chunks = [c async for c in MoonshineSTT(transcriber_factory=lambda: fake).stream(audio())]
    assert [c.text for c in chunks] == ["same", "hello world"]
