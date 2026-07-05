"""Composition root: `uv run python -m voicecode.server`.

The only module here that imports the concrete bridge, substrate, and providers.
Boot order: workstream settings file (per-boot approval token) → store → tmux
session "voice" → convo session (reuse/resume/spawn) → bridge → app → uvicorn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sys
from pathlib import Path

import uvicorn

from voicecode import keychain
from voicecode.adapters.cartesia_tts import CartesiaTTS
from voicecode.adapters.deepgram_stt import DeepgramSTT
from voicecode.audio.pipeline import AudioPipeline
from voicecode.bootstrap import ensure_convo, fresh_convo
from voicecode.config import Config
from voicecode.convo import ConvoBridge
from voicecode.server.app import create_app
from voicecode.server.logs import setup_logging
from voicecode.server.store import Store
from voicecode.substrate import Substrate, Tmux

REPO = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = REPO / "plugins" / "claude-code"
ASK_PHONE = REPO / "hooks" / "ask_phone.py"


def _secret(name: str) -> str:
    value = keychain.get_secret(name)
    if not value:
        env = name.upper().replace("-", "_")
        raise RuntimeError(f"missing secret {name!r}: run /voice-code:deploy or set ${env}")
    return value


def _write_workstream_settings(config: Config, token: str) -> Path:
    """Per-boot settings file wiring every workstream's Bash calls through the
    phone-approval relay."""
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f"{sys.executable} {ASK_PHONE} --gate-bash"
                                f" --port {config.port} --token {token}"
                            ),
                            "timeout": 120,
                        }
                    ],
                }
            ]
        }
    }
    path = config.data_dir / "workstream-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))
    return path


async def _serve() -> None:
    config = Config.load()
    setup_logging()
    token = secrets.token_urlsafe(32)
    settings_path = _write_workstream_settings(config, token)
    store = Store(config.db_path)
    tmux = Tmux()
    substrate = Substrate(tmux, Path.home())
    await tmux.ensure_session("voice")
    convo = await ensure_convo(store, substrate, PLUGIN_DIR)
    bridge = ConvoBridge(substrate, convo)
    bridge_task = asyncio.create_task(bridge.run())

    def stt_factory() -> DeepgramSTT:
        return DeepgramSTT(api_key=_secret("deepgram-api-key"))

    def tts_factory() -> CartesiaTTS:
        return CartesiaTTS(api_key=_secret("cartesia-api-key"))

    def pipeline_factory(stt, tts, convo_bridge, sink) -> AudioPipeline:
        return AudioPipeline(stt, tts, convo_bridge, sink)

    async def convo_reset() -> Path:
        session = await fresh_convo(store, substrate, PLUGIN_DIR)
        bridge.reset(session)
        return session.transcript

    app = create_app(
        config,
        store=store,
        bridge=bridge,
        substrate=substrate,
        convo_transcript=convo.transcript,
        stt_factory=stt_factory,
        tts_factory=tts_factory,
        pipeline_factory=pipeline_factory,
        convo_reset=convo_reset,
        approvals_token=token,
        plugin_dir=PLUGIN_DIR,
        workstream_settings=settings_path,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_config=None)
    )
    try:
        await server.serve()
    finally:
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        await bridge.close()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
