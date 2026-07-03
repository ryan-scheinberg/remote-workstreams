"""Deepgram Nova-3 streaming STT with endpointing.

STUB — replaced by the audio unit. The class name and module path are frozen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voicecode.adapters.stt import STTAdapter, TranscriptChunk


class DeepgramSTT(STTAdapter):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptChunk]:
        raise NotImplementedError
