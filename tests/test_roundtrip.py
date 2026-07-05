"""Round-trip check plumbing: resampler math and the no-keys refusal.
The live TTS→STT path needs real keys and runs via the deploy plugin, not here."""

from __future__ import annotations

from array import array

from remote_workstreams.audio import roundtrip


def test_resample_downsamples_24k_to_16k() -> None:
    src = array("h", range(-120, 120)).tobytes()  # 240 samples
    out = roundtrip.resample_s16le(src, 24000, 16000)
    assert len(out) == 2 * int(240 * 16000 / 24000)


def test_resample_same_rate_is_identity() -> None:
    src = array("h", [0, 1000, -1000, 32767]).tobytes()
    assert roundtrip.resample_s16le(src, 16000, 16000) == src


def test_resample_preserves_constant_signal() -> None:
    src = array("h", [5000] * 240).tobytes()
    out = array("h")
    out.frombytes(roundtrip.resample_s16le(src, 24000, 16000))
    assert all(sample == 5000 for sample in out)


def test_resample_empty_input() -> None:
    assert roundtrip.resample_s16le(b"", 24000, 16000) == b""


async def test_refuses_without_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(roundtrip.keychain, "get_secret", lambda name: None)
    assert await roundtrip.main() == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "deepgram-api-key" in out and "cartesia-api-key" in out
