"""Small PCM helpers shared by local and hosted audio providers."""

from __future__ import annotations

from array import array


def resample_s16le(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linearly resample mono signed-16 little-endian PCM."""
    src = array("h")
    src.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not src or src_rate == dst_rate:
        return src.tobytes()
    n_out = max(int(len(src) * dst_rate / src_rate), 1)
    out = array("h", [0]) * n_out
    step = (len(src) - 1) / max(n_out - 1, 1)
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        nxt = src[j + 1] if j + 1 < len(src) else src[j]
        out[i] = int(src[j] * (1 - frac) + nxt * frac)
    return out.tobytes()


def s16le_to_float32(pcm: bytes) -> list[float]:
    """Convert signed-16 little-endian mono PCM to model input samples."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return [sample / 32768.0 for sample in samples]


def float32_to_s16le(samples: object) -> bytes:
    """Convert model float samples in [-1, 1] to signed-16 PCM."""
    out = array("h")
    for sample in samples:  # type: ignore[union-attr]
        value = max(-1.0, min(1.0, float(sample)))
        out.append(int(value * 32767.0))
    return out.tobytes()
