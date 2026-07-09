"""Local ambient mode: Mac mic + speaker, no phone. `uv run python -m remote_workstreams.ambient`.

Composition root — real tmux Substrate + the `voice:convo` Claude Code session,
bridged by ConvoBridge into an AudioPipeline with Deepgram STT, Cartesia TTS, and a
sounddevice sink. Transcript entries print to stdout. sounddevice imports are lazy
so importing this module never touches audio devices.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Literal

from remote_workstreams import keychain
from remote_workstreams.adapters.cartesia_tts import CartesiaTTS
from remote_workstreams.adapters.deepgram_stt import DeepgramSTT
from remote_workstreams.audio.pipeline import AudioPipeline
from remote_workstreams.audio.state import PipelineState
from remote_workstreams.bootstrap import ensure_convo
from remote_workstreams.config import Config
from remote_workstreams.convo import ConvoBridge
from remote_workstreams.protocol import MIC_FORMAT, TTS_FORMAT
from remote_workstreams.server.store import Store
from remote_workstreams.substrate import Substrate, Tmux
from remote_workstreams.transcript import AssistantText, ToolActivity, UserText

MIC_BLOCK_FRAMES = MIC_FORMAT.sample_rate // 20  # 50ms mic blocks

SECRETS = ("deepgram-api-key", "cartesia-api-key")

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "claude-code"


async def print_entries(bridge: ConvoBridge) -> None:
    async for entry in bridge.subscribe():
        if isinstance(entry, UserText):
            print(f"user: {entry.text}")
        elif isinstance(entry, AssistantText):
            print(f"assistant: {entry.text}")
        elif isinstance(entry, ToolActivity):
            print(f"[{entry.label}]")


class LocalAudioSink:
    """Mac speaker for TTS PCM; states to stdout. INTERRUPTED aborts the speaker
    buffer so playback dies the moment barge-in lands. Chat text comes from the
    transcript via print_entries, not from here — transcript() is interims only."""

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
        pass

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
        print("Set the matching env vars or store them via /remote-workstreams:deploy.")
        return 2

    config = Config.load()
    store = Store(config.db_path)
    substrate = Substrate(Tmux(), home=Path.home(), codex_command=config.codex_command)
    session = await ensure_convo(store, substrate, PLUGIN_DIR)
    bridge = ConvoBridge(substrate, session)

    sink = LocalAudioSink()
    pipeline = AudioPipeline(
        stt=DeepgramSTT(secrets["deepgram-api-key"]),
        tts=CartesiaTTS(secrets["cartesia-api-key"]),
        convo=bridge,
        sink=sink,
    )
    tasks = [asyncio.create_task(bridge.run()), asyncio.create_task(print_entries(bridge))]

    mic = start_mic(asyncio.get_running_loop(), pipeline)
    print(f"remote-workstreams ambient — convo at {session.window} (Ctrl-C to quit)")
    try:
        await pipeline.run()
    finally:
        mic.stop()
        mic.close()
        await pipeline.close()
        await bridge.close()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sink.close()
        store.close()
    return 0


def run() -> int:
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(run())
