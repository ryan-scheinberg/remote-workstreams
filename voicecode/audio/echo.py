"""Server-side echo suppression: drop mic transcripts that are the phone
replaying our own TTS through its speaker.

Browser echoCancellation proved unreliable on iOS — the agent barged in on
itself and transcribed its own opening words as the user's. The pipeline knows
what it spoke and how many seconds of PCM it shipped, so a transcript arriving
while the phone could still be playing it is a candidate for echo.

The signal is a VERBATIM CONTIGUOUS RUN: real acoustic echo transcribes a long
run of the reply word-for-word, while a human reply reuses the topic's words but
never quotes five of them in a row in order. So echo = some 5-word run of the
transcript appears verbatim in what we just said. Anything shorter than that run
always passes — barge-ins ("wait, stop that") and on-topic replies are never
eaten. Erring this way is deliberate: a rare leaked echo self-interrupts one
turn (recoverable — the reply still lands in chat), but eating real speech
breaks the conversation.
"""

from __future__ import annotations

import time
from collections.abc import Callable

_MARGIN_S = 1.5  # slack past computed playback end: network + client buffering
_RUN = 5  # verbatim consecutive words that mark echo, not a human reply


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
        if len(words) < _RUN:  # short utterances (incl. barge-ins) always pass
            return False
        spoken = self._spoken.split()
        grams = {tuple(spoken[j : j + _RUN]) for j in range(len(spoken) - _RUN + 1)}
        return any(tuple(words[i : i + _RUN]) in grams for i in range(len(words) - _RUN + 1))
