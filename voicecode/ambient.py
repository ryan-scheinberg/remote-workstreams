"""Local ambient mode: Mac mic + speaker, no phone. `uv run python -m voicecode.ambient`.

Composition root — wires AsyncAnthropic + ConversationEngine + ClaudeCodeAdapter +
DeepgramSTT + CartesiaTTS into an AudioPipeline with a sounddevice sink, plus an
execution-events consumer pumping adapter.events() into the pipeline. Transcripts
and pipeline states print to stdout. sounddevice imports are lazy so importing this
module never touches audio devices.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from typing import Literal

from anthropic import AsyncAnthropic

from voicecode import keychain
from voicecode.adapters.cartesia_tts import CartesiaTTS
from voicecode.adapters.claude_code import ClaudeCodeAdapter
from voicecode.adapters.deepgram_stt import DeepgramSTT
from voicecode.adapters.execution import ExecutionAdapter
from voicecode.audio.pipeline import AudioPipeline
from voicecode.audio.state import PipelineState
from voicecode.config import Config
from voicecode.engine.conversation import ConversationEngine
from voicecode.protocol import MIC_FORMAT, TTS_FORMAT

MIC_BLOCK_FRAMES = MIC_FORMAT.sample_rate // 20  # 50ms mic blocks

SECRETS = ("anthropic-api-key", "deepgram-api-key", "cartesia-api-key")


class Dispatcher:
    """on_dispatch policy: adapter.start() on the first directive, send() after.
    on_started fires once, right after start(), so main can attach the events pump."""

    def __init__(self, adapter: ExecutionAdapter) -> None:
        self.adapter = adapter
        self.session_id: str | None = None
        self.on_started: Callable[[], None] | None = None

    async def __call__(self, directive: str) -> None:
        if self.session_id is None:
            self.session_id = await self.adapter.start(directive)
            print(f"[execution session {self.session_id}]")
            if self.on_started is not None:
                self.on_started()
        else:
            await self.adapter.send(directive)


async def pump_events(adapter: ExecutionAdapter, pipeline: AudioPipeline) -> None:
    async for event in adapter.events():
        print(f"[{event.type}] {event.summary}")
        await pipeline.on_events([event])


class LocalAudioSink:
    """Mac speaker for TTS PCM; transcripts and states to stdout. INTERRUPTED aborts
    the speaker buffer so playback dies the moment barge-in lands."""

    def __init__(self) -> None:
        import sounddevice as sd

        self._out = sd.RawOutputStream(
            samplerate=TTS_FORMAT.sample_rate,
            channels=TTS_FORMAT.channels,
            dtype="int16",
        )
        self._out.start()

    async def state(self, state: PipelineState) -> None:
        print(f"[{state.value}]")
        if state is PipelineState.INTERRUPTED:
            await asyncio.to_thread(self._out.abort)  # drop buffered audio now
            self._out.start()

    async def transcript(self, role: Literal["user", "assistant"], text: str, final: bool) -> None:
        if final:
            print(f"{role}: {text}")

    async def audio(self, pcm: bytes) -> None:
        await asyncio.to_thread(self._out.write, pcm)  # write blocks; keep the loop free

    async def speech_end(self) -> None:
        pass

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._out.abort()
            self._out.close()


def start_mic(loop: asyncio.AbstractEventLoop, pipeline: AudioPipeline) -> object:
    import sounddevice as sd

    def on_block(indata, frames, time_info, status) -> None:  # PortAudio thread
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        asyncio.run_coroutine_threadsafe(pipeline.feed(bytes(indata)), loop)

    stream = sd.RawInputStream(
        samplerate=MIC_FORMAT.sample_rate,
        channels=MIC_FORMAT.channels,
        dtype="int16",
        blocksize=MIC_BLOCK_FRAMES,
        callback=on_block,
    )
    stream.start()
    return stream


async def main() -> int:
    secrets = {name: keychain.get_secret(name) for name in SECRETS}
    missing = [name for name, value in secrets.items() if not value]
    if missing:
        print(f"Missing secrets: {', '.join(missing)}.")
        print("Set the matching env vars or store them via /voice-code:deploy.")
        return 2

    config = Config.load()
    engine = ConversationEngine(
        AsyncAnthropic(api_key=secrets["anthropic-api-key"]),
        model=config.conversation_model,
    )
    adapter = ClaudeCodeAdapter(config)
    dispatcher = Dispatcher(adapter)
    sink = LocalAudioSink()
    pipeline = AudioPipeline(
        stt=DeepgramSTT(secrets["deepgram-api-key"]),
        tts=CartesiaTTS(secrets["cartesia-api-key"]),
        engine=engine,
        sink=sink,
        on_dispatch=dispatcher,
    )
    tasks: list[asyncio.Task[None]] = []
    dispatcher.on_started = lambda: tasks.append(
        asyncio.create_task(pump_events(adapter, pipeline))
    )

    mic = start_mic(asyncio.get_running_loop(), pipeline)
    print("voice-code ambient — listening (Ctrl-C to quit)")
    try:
        await pipeline.run()
    finally:
        mic.stop()
        mic.close()
        await pipeline.close()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if dispatcher.session_id is not None:
            with contextlib.suppress(Exception):
                await adapter.stop()
        sink.close()
    return 0


def run() -> int:
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(run())
