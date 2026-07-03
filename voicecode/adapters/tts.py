"""Streaming TTS behind an adapter. Cartesia is the v1 implementation
(ElevenLabs later). Sentence-chunked input, PCM out, cancel() for barge-in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from voicecode.protocol import TTS_FORMAT, AudioFormat


class TTSAdapter(ABC):
    format: AudioFormat = TTS_FORMAT

    @abstractmethod
    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize one sentence chunk; yield PCM as soon as the provider streams it."""

    @abstractmethod
    async def cancel(self) -> None:
        """Barge-in: abort in-flight synthesis immediately. Idempotent."""
