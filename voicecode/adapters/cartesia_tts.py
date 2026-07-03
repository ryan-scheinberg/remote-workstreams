"""Cartesia streaming TTS.

STUB — replaced by the audio unit. The class name and module path are frozen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voicecode.adapters.tts import TTSAdapter


class CartesiaTTS(TTSAdapter):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError
