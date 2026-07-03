"""Headless audio round-trip check: real Cartesia TTS → resample → real Deepgram STT.

`uv run python -m voicecode.audio.roundtrip` — the deploy plugin's final connection
test. Synthesizes a short utterance, feeds it back through streaming STT, and asserts
a sane transcript round-trips. Requires live keys; refuses clearly when missing.
Exit codes: 0 pass, 1 fail, 2 keys missing.
"""

from __future__ import annotations

import asyncio
import sys
import time
from array import array
from collections.abc import AsyncIterator

from voicecode import keychain
from voicecode.adapters.cartesia_tts import CartesiaTTS
from voicecode.adapters.deepgram_stt import DeepgramSTT
from voicecode.protocol import MIC_FORMAT, TTS_FORMAT

PHRASE = "Voice code round trip test."
EXPECT_WORDS = {"voice", "code", "round", "trip", "test"}
CHUNK_MS = 50
SILENCE_TAIL_S = 1.5  # > endpointing window so Deepgram commits the utterance
TIMEOUT_S = 30.0


def resample_s16le(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of mono s16le PCM (audioop is gone in 3.13)."""
    src = array("h")
    src.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not src or src_rate == dst_rate:
        return src.tobytes()
    n_out = max(int(len(src) * dst_rate / src_rate), 1)
    out = array("h", bytes(2 * n_out))
    step = (len(src) - 1) / max(n_out - 1, 1)
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        nxt = src[j + 1] if j + 1 < len(src) else src[j]
        out[i] = int(src[j] * (1 - frac) + nxt * frac)
    return out.tobytes()


async def main() -> int:
    dg_key = keychain.get_secret("deepgram-api-key")
    ct_key = keychain.get_secret("cartesia-api-key")
    missing = [
        name for name, value in (("deepgram-api-key", dg_key), ("cartesia-api-key", ct_key))
        if not value
    ]
    if missing:
        print(f"REFUSED: missing {', '.join(missing)}.")
        print("Set DEEPGRAM_API_KEY / CARTESIA_API_KEY or store them via /voice-code:deploy.")
        return 2

    tts = CartesiaTTS(ct_key)
    tts_start = time.time()
    first_audio: float | None = None
    parts: list[bytes] = []
    async for pcm in tts.synthesize(PHRASE):
        if first_audio is None:
            first_audio = time.time()
        parts.append(pcm)
    if not parts:
        print("FAIL: Cartesia returned no audio.")
        return 1
    tts_ms = (first_audio - tts_start) * 1000
    mic_pcm = resample_s16le(b"".join(parts), TTS_FORMAT.sample_rate, MIC_FORMAT.sample_rate)

    chunk_bytes = MIC_FORMAT.sample_rate * 2 * CHUNK_MS // 1000

    async def mic() -> AsyncIterator[bytes]:
        for i in range(0, len(mic_pcm), chunk_bytes):
            yield mic_pcm[i : i + chunk_bytes]
        for _ in range(int(SILENCE_TAIL_S * 1000 / CHUNK_MS)):
            yield b"\x00" * chunk_bytes

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
        print("FAIL: no Deepgram endpoint within timeout.")
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
