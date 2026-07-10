"""Headless audio round-trip check using the configured voice providers.

`uv run python -m remote_workstreams.audio.roundtrip` — the deploy plugin's final connection
test. Synthesizes a short utterance, feeds it back through streaming STT, and asserts
a sane transcript round-trips. Cloud providers require live keys; Moonshine is keyless.
Exit codes: 0 pass, 1 fail, 2 keys missing.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator

from remote_workstreams import keychain
from remote_workstreams.adapters.cartesia_tts import CartesiaTTS
from remote_workstreams.adapters.deepgram_stt import DeepgramSTT
from remote_workstreams.adapters.moonshine_stt import MoonshineSTT
from remote_workstreams.adapters.moonshine_tts import MoonshineTTS
from remote_workstreams.audio.pcm import resample_s16le
from remote_workstreams.config import Config
from remote_workstreams.protocol import MIC_FORMAT, TTS_FORMAT

PHRASE = "Voice code round trip test."
EXPECT_WORDS = {"voice", "code", "round", "trip", "test"}
CHUNK_MS = 50
SILENCE_TAIL_S = 1.5  # > endpointing window so Deepgram commits the utterance
TIMEOUT_S = 30.0


async def main() -> int:
    config = Config.load()
    missing: list[str] = []
    if config.tts_provider != "moonshine" and not keychain.get_secret("cartesia-api-key"):
        missing.append("cartesia-api-key")
    if config.stt_provider != "moonshine" and not keychain.get_secret("deepgram-api-key"):
        missing.append("deepgram-api-key")
    if missing:
        print(f"REFUSED: missing {', '.join(missing)}.")
        return 2

    if config.tts_provider == "moonshine":
        tts = MoonshineTTS(
            locale=config.moonshine_tts_locale,
            voice=config.moonshine_tts_voice,
            speed=config.moonshine_tts_speed,
            model_dir=config.moonshine_model_dir / "tts",
        )
    else:
        ct_key = keychain.get_secret("cartesia-api-key")
        assert ct_key is not None
        tts = CartesiaTTS(ct_key)
    tts_start = time.time()
    first_audio: float | None = None
    parts: list[bytes] = []
    async for pcm in tts.synthesize(PHRASE):
        if first_audio is None:
            first_audio = time.time()
        parts.append(pcm)
    if not parts:
        print("FAIL: configured TTS provider returned no audio.")
        return 1
    tts_ms = (first_audio - tts_start) * 1000
    mic_pcm = resample_s16le(b"".join(parts), TTS_FORMAT.sample_rate, MIC_FORMAT.sample_rate)

    chunk_bytes = MIC_FORMAT.sample_rate * 2 * CHUNK_MS // 1000

    async def mic() -> AsyncIterator[bytes]:
        for i in range(0, len(mic_pcm), chunk_bytes):
            yield mic_pcm[i : i + chunk_bytes]
        for _ in range(int(SILENCE_TAIL_S * 1000 / CHUNK_MS)):
            yield b"\x00" * chunk_bytes

    if config.stt_provider == "moonshine":
        stt = MoonshineSTT(
            language=config.moonshine_language,
            model=config.moonshine_stt_model,
            model_dir=config.moonshine_model_dir / "stt",
        )
    else:
        dg_key = keychain.get_secret("deepgram-api-key")
        assert dg_key is not None
        stt = DeepgramSTT(dg_key)
    stt_start = time.time()
    finals: list[str] = []
    try:
        async with asyncio.timeout(TIMEOUT_S):
            async for chunk in stt.stream(mic()):
                if chunk.is_final and chunk.text:
                    finals.append(chunk.text)
                if chunk.speech_final and finals:
                    break
    except TimeoutError:
        provider = "Moonshine" if config.stt_provider == "moonshine" else "Deepgram"
        print(f"FAIL: no {provider} endpoint within timeout.")
        return 1
    stt_ms = (time.time() - stt_start) * 1000

    transcript = " ".join(finals).lower()
    heard = {word.strip(".,!?") for word in transcript.split()}
    matched = EXPECT_WORDS & heard
    ok = len(matched) >= 3
    print(f"transcript: {transcript!r}")
    print(f"tts first audio: {tts_ms:.0f}ms  stt to endpoint: {stt_ms:.0f}ms")
    if ok:
        print("PASS")
        return 0
    print(f"FAIL: expected words from {sorted(EXPECT_WORDS)}, matched {sorted(matched)}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
