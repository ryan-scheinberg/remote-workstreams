"""Deepgram Nova-3 streaming STT with endpointing (deepgram-sdk v7 websocket API).

The class name and module path are frozen. Endpointing is Deepgram's: a Results
message with speech_final=True is the "user stopped speaking" decision; an
UtteranceEnd message (fired ~UTTERANCE_END_MS after the last word when speech_final
was missed, e.g. trailing noise) maps to an empty speech_final chunk so the pipeline
can commit accumulated finals.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from deepgram import AsyncDeepgramClient

from voicecode.adapters.stt import STTAdapter, TranscriptChunk
from voicecode.protocol import MIC_FORMAT

ENDPOINTING_MS = 700  # trailing silence before "done" — room for a thinking pause mid-sentence
UTTERANCE_END_MS = 1500  # word-gap backstop; keep above ENDPOINTING_MS
KEEPALIVE_INTERVAL = 5.0  # Deepgram drops sockets idle ~10s (e.g. while muted)


class DeepgramSTT(STTAdapter):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptChunk]:
        client = AsyncDeepgramClient(api_key=self.api_key)
        async with client.listen.v1.connect(
            model="nova-3",
            encoding="linear16",
            sample_rate=MIC_FORMAT.sample_rate,
            channels=MIC_FORMAT.channels,
            interim_results=True,
            endpointing=ENDPOINTING_MS,
            utterance_end_ms=UTTERANCE_END_MS,
        ) as connection:
            pump = asyncio.create_task(self._pump(audio, connection))
            keepalive = asyncio.create_task(self._keepalive(connection))
            try:
                async for message in connection:
                    kind = getattr(message, "type", None)
                    if kind == "Results":
                        alternatives = message.channel.alternatives
                        text = alternatives[0].transcript if alternatives else ""
                        yield TranscriptChunk(
                            text=text or "",
                            is_final=bool(message.is_final),
                            speech_final=bool(message.speech_final),
                        )
                    elif kind == "UtteranceEnd":
                        yield TranscriptChunk(text="", is_final=True, speech_final=True)
            finally:
                for task in (pump, keepalive):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _pump(self, audio: AsyncIterator[bytes], connection: object) -> None:
        try:
            async for chunk in audio:
                await connection.send_media(chunk)
        finally:
            # Mic ended (pipeline close): ask Deepgram to flush final results and close,
            # which ends the message iteration above.
            with contextlib.suppress(Exception):
                await connection.send_close_stream()

    async def _keepalive(self, connection: object) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            with contextlib.suppress(Exception):
                await connection.send_keep_alive()
