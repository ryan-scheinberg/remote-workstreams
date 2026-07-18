from __future__ import annotations

import asyncio
import threading

from remote_workstreams.adapters.moonshine_tts import MoonshineTTS
from remote_workstreams.protocol import TTS_FORMAT


class FakeSynthesizer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def synthesize(self, text: str, *, speed: float):
        self.started.set()
        self.release.wait(1)
        return [0.0, 0.5, -0.5, 0.0] * 2000, 16000


async def test_synthesize_resamples_and_frames_pcm() -> None:
    class ReadySynth:
        def synthesize(self, text: str, *, speed: float):
            return [0.0, 0.5, -0.5, 0.0] * 100, 16000

    tts = MoonshineTTS(synthesizer_factory=ReadySynth)
    frames = [frame async for frame in tts.synthesize("hello")]
    pcm = b"".join(frames)
    assert pcm
    assert all(len(frame) <= TTS_FORMAT.sample_rate * 2 * 40 // 1000 for frame in frames)
    assert len(pcm) > 200


async def test_cancel_discards_late_synthesis_result() -> None:
    fake = FakeSynthesizer()
    tts = MoonshineTTS(synthesizer_factory=lambda: fake)
    task = asyncio.create_task(tts.synthesize("hello").__anext__())
    await asyncio.to_thread(fake.started.wait, 1)
    await tts.cancel()
    fake.release.set()
    try:
        await task
    except StopAsyncIteration:
        pass
    else:  # pragma: no cover - the adapter must suppress the late result
        raise AssertionError("cancelled synthesis yielded audio")
