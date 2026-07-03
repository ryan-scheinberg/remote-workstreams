"""Composition root: `uv run python -m voicecode.server`.

The only module that imports the concrete engine, adapters, and pipeline.
"""

from __future__ import annotations

import uvicorn
from anthropic import AsyncAnthropic

from voicecode import keychain
from voicecode.adapters.cartesia_tts import CartesiaTTS
from voicecode.adapters.claude_code import ClaudeCodeAdapter
from voicecode.adapters.deepgram_stt import DeepgramSTT
from voicecode.adapters.stt import STTAdapter
from voicecode.adapters.tts import TTSAdapter
from voicecode.audio.pipeline import AudioPipeline, AudioSink
from voicecode.config import Config
from voicecode.engine.conversation import ConversationEngine
from voicecode.server.app import create_app
from voicecode.server.logs import setup_logging
from voicecode.server.sessions import OnDispatch


def _secret(name: str) -> str:
    value = keychain.get_secret(name)
    if not value:
        env = name.upper().replace("-", "_")
        raise RuntimeError(f"missing secret {name!r}: run /voice-code:deploy or set ${env}")
    return value


def main() -> None:
    config = Config.load()
    setup_logging()

    def engine_factory() -> ConversationEngine:
        client = AsyncAnthropic(api_key=_secret("anthropic-api-key"))
        return ConversationEngine(client, model=config.conversation_model)

    def execution_factory() -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(config)

    def stt_factory() -> DeepgramSTT:
        return DeepgramSTT(api_key=_secret("deepgram-api-key"))

    def tts_factory() -> CartesiaTTS:
        return CartesiaTTS(api_key=_secret("cartesia-api-key"))

    def pipeline_factory(
        stt: STTAdapter,
        tts: TTSAdapter,
        engine: ConversationEngine,
        sink: AudioSink,
        on_dispatch: OnDispatch,
    ) -> AudioPipeline:
        return AudioPipeline(stt, tts, engine, sink, on_dispatch=on_dispatch)

    app = create_app(
        config,
        engine_factory=engine_factory,
        execution_factory=execution_factory,
        stt_factory=stt_factory,
        tts_factory=tts_factory,
        pipeline_factory=pipeline_factory,
    )
    uvicorn.run(app, host=config.host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
