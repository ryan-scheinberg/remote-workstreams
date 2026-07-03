"""Cartesia streaming TTS (cartesia 3.3.0, SSE path: POST /tts/sse).

The class name and module path are frozen. Each synthesize() call streams one
sentence chunk as raw pcm_s16le 24kHz (protocol.TTS_FORMAT); cancel() aborts the
in-flight HTTP stream and is idempotent. The api key comes from
keychain.get_secret("cartesia-api-key") at composition and is passed in here.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from cartesia import AsyncCartesia

from voicecode.adapters.tts import TTSAdapter

# No default voice exists in the SDK; this is Cartesia's stock "Barbershop Man".
# Swap via the voice_id constructor arg once Ryan picks a voice on the real account.
DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"
DEFAULT_MODEL_ID = "sonic-3"


class CartesiaTTS(TTSAdapter):
    def __init__(
        self,
        api_key: str,
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self._client: AsyncCartesia | None = None
        self._stream: object | None = None  # in-flight AsyncStream, if any

    def _ensure_client(self) -> AsyncCartesia:
        if self._client is None:
            self._client = AsyncCartesia(api_key=self.api_key)
        return self._client

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        stream = await self._ensure_client().tts.generate_sse(
            model_id=self.model_id,
            transcript=text,
            voice={"mode": "id", "id": self.voice_id},
            output_format={
                "container": "raw",
                "encoding": self.format.encoding,
                "sample_rate": self.format.sample_rate,
            },
        )
        self._stream = stream
        try:
            async for event in stream:
                if self._stream is not stream:
                    break  # cancelled
                kind = getattr(event, "type", None)
                if kind == "chunk":
                    pcm = event.audio  # base64-decoded by the SDK
                    if pcm:
                        yield pcm
                elif kind == "error":
                    raise RuntimeError(f"cartesia synthesis failed: {event}")
                elif kind == "done":
                    break
        except Exception:
            if self._stream is stream:
                raise
            # cancel() closed the stream under us; teardown noise is expected
        finally:
            if self._stream is stream:
                self._stream = None
            with contextlib.suppress(Exception):
                await stream.close()

    async def cancel(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()  # aborts the HTTP response mid-stream
