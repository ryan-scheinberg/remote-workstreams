"""On-device Moonshine Voice TTS using Kokoro or Piper voices."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from remote_workstreams.adapters.tts import TTSAdapter
from remote_workstreams.audio.pcm import float32_to_s16le, resample_s16le
from remote_workstreams.protocol import TTS_FORMAT

_FRAME_BYTES = TTS_FORMAT.sample_rate * 2 * 40 // 1000


def _default_synthesizer(
    locale: str, voice: str, model_dir: Path
) -> object:
    try:
        from moonshine_voice import TextToSpeech
    except ImportError as exc:  # pragma: no cover - exercised by deployment checks
        raise RuntimeError(
            "Moonshine is not installed; run `uv sync --extra local-voice`."
        ) from exc
    model_dir.mkdir(parents=True, exist_ok=True)
    return TextToSpeech(locale, voice=voice, asset_root=model_dir, download=True)


class MoonshineTTS(TTSAdapter):
    """Sentence-level local synthesis with immediate cancellation semantics."""

    def __init__(
        self,
        *,
        locale: str = "en-us",
        voice: str = "kokoro_af_heart",
        speed: float = 1.0,
        model_dir: Path | str | None = None,
        synthesizer_factory: Callable[[], object] | None = None,
    ) -> None:
        self.locale = locale
        self.voice = voice
        self.speed = speed
        self.model_dir = Path(model_dir or Path.home() / ".remote-workstreams/models/moonshine-tts")
        self._factory = synthesizer_factory
        self._synthesizer: object | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    def _get_synthesizer(self) -> object:
        if self._synthesizer is None:
            self._synthesizer = (
                self._factory() if self._factory is not None else _default_synthesizer(
                    self.locale, self.voice, self.model_dir
                )
            )
        return self._synthesizer

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with self._lock:
            token = self._generation
            synthesizer = await asyncio.to_thread(self._get_synthesizer)
            samples, sample_rate = await asyncio.to_thread(
                synthesizer.synthesize, text, speed=self.speed
            )
            if token != self._generation:
                return
            pcm = float32_to_s16le(samples)
            pcm = resample_s16le(pcm, int(sample_rate), TTS_FORMAT.sample_rate)
            for offset in range(0, len(pcm), _FRAME_BYTES):
                if token != self._generation:
                    return
                yield pcm[offset : offset + _FRAME_BYTES]

    async def cancel(self) -> None:
        self._generation += 1

    async def close(self) -> None:
        self._generation += 1
        close = getattr(self._synthesizer, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(close)
        self._synthesizer = None
