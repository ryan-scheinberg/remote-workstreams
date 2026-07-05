"""Server-side echo suppression: drop mic transcripts that are the phone
replaying our own TTS through its speaker.

Browser echoCancellation proved unreliable on iOS — the agent barged in on
itself and transcribed its own opening words as the user's. The pipeline knows
exactly what it spoke and how many seconds of PCM it shipped, so a transcript
that textually matches the current utterance, arriving while the phone could
still be playing it, is echo. Matching is ordered-substring over normalized
words: real interruptions ("wait", "stop that") don't mirror the reply
word-for-word, so they pass through and barge in as usual.
"""

from __future__ import annotations

import time
from collections.abc import Callable

_MARGIN_S = 1.5  # slack past computed playback end: network + client buffering


def _norm(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return " ".join(cleaned.split())


class EchoGuard:
    """Tracks one utterance at a time; the pipeline resets it per turn."""

    def __init__(self, sample_rate: int = 24000, now: Callable[[], float] = time.time) -> None:
        self._bytes_per_second = sample_rate * 2  # s16le mono
        self._now = now
        self._spoken = ""
        self._first_audio: float | None = None
        self._audio_bytes = 0

    def start_utterance(self) -> None:
        self._spoken = ""
        self._first_audio = None
        self._audio_bytes = 0

    def note_sentence(self, text: str) -> None:
        norm = _norm(text)
        if norm:
            self._spoken = f"{self._spoken} {norm}".strip()

    def note_audio(self, nbytes: int) -> None:
        if self._first_audio is None:
            self._first_audio = self._now()
        self._audio_bytes += nbytes

    def cut_off(self) -> None:
        """Barge-in flushed the client's buffer; unplayed audio can't echo."""
        if self._first_audio is None:
            return
        played = int((self._now() - self._first_audio) * self._bytes_per_second)
        self._audio_bytes = min(self._audio_bytes, played)

    def is_echo(self, text: str) -> bool:
        if self._first_audio is None or not self._spoken:
            return False
        playback_end = self._first_audio + self._audio_bytes / self._bytes_per_second
        if self._now() > playback_end + _MARGIN_S:
            return False
        words = _norm(text).split()
        if not words:
            return False
        # STT mishears its own speaker ("brian" for "ryan"), so exact-sequence
        # matching whiffs; majority word overlap is the workable signal.
        spoken = set(self._spoken.split())
        hits = sum(1 for w in words if w in spoken)
        return hits / len(words) >= 0.6
