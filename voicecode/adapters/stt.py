"""Streaming STT behind an adapter. Deepgram Nova-3 is the v1 implementation;
endpointing (the "user stopped speaking" decision) is the adapter's job and
surfaces as speech_final=True.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field


class TranscriptChunk(BaseModel):
    text: str
    is_final: bool  # this span's transcript will not change
    speech_final: bool  # endpoint: the user has stopped speaking — commit the turn
    ts: float = Field(default_factory=time.time)


class STTAdapter(ABC):
    @abstractmethod
    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptChunk]:
        """Consume mic PCM (protocol.MIC_FORMAT), yield transcript chunks."""
