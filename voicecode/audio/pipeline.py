"""AudioPipeline drives one live audio session: mic PCM in → STT → engine turn →
sentence-chunked TTS → PCM out, with barge-in killing TTS the instant user speech
is detected during SPEAKING.

STUB — replaced by the audio unit. AudioPipeline's public surface and AudioSink
are frozen; the server and local frontend code against them. Per-turn latency
instrumentation (endpoint→transcript→TTFT→first-audio timestamps, structured log)
lives inside this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from voicecode.adapters.stt import STTAdapter
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.state import PipelineState
from voicecode.engine.conversation import ConversationEngine
from voicecode.events import StatusEvent


class AudioSink(Protocol):
    """Where pipeline output goes — a WebSocket connection or the local speaker."""

    async def state(self, state: PipelineState) -> None: ...

    async def transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None: ...

    async def audio(self, pcm: bytes) -> None: ...

    async def speech_end(self) -> None: ...


class AudioPipeline:
    def __init__(
        self,
        stt: STTAdapter,
        tts: TTSAdapter,
        engine: ConversationEngine,
        sink: AudioSink,
    ) -> None:
        self.stt = stt
        self.tts = tts
        self.engine = engine
        self.sink = sink
        self.state = PipelineState.LISTENING
        self.muted = False

    async def run(self) -> None:
        """Main loop; consumes audio fed via feed() until close()."""
        raise NotImplementedError

    async def feed(self, pcm: bytes) -> None:
        """Mic audio from the client (protocol.MIC_FORMAT)."""
        raise NotImplementedError

    async def text(self, text: str) -> None:
        """Typed input; skips STT, flows through the same turn machinery."""
        raise NotImplementedError

    async def on_events(self, events: Sequence[StatusEvent]) -> None:
        """Bridge input: inject into the engine; completed/needs_approval trigger
        proactive speech when the user is silent and not muted."""
        raise NotImplementedError

    def set_muted(self, muted: bool) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
