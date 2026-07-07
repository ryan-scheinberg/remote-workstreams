"""Server-side echo suppression: drop mic transcripts that are the phone
replaying our own TTS through its speaker.

Browser echoCancellation proved unreliable on iOS — the agent barged in on
itself and transcribed its own words as the user's. The pipeline knows what it
spoke and how many seconds of PCM it shipped, so a transcript arriving while
the phone could still be playing it is a candidate for echo.

Echo has three observed shapes, and verbatim matching only catches the first:
1. Verbatim: a long word-for-word run, or the reply's opening as a clipped
   capture (barge-in kills playback at the opening; those words then endpoint
   as phantom input).
2. Phonetic garble: Deepgram mishears the replayed audio — "Yep, Sonnet 5"
   came back as "yep it's on at five", and "5" is spelled out as "five". No
   verbatim run survives that, so garbles are caught by character-level
   similarity of digit-normalized text: against the opening when the
   transcript starts with the reply's exact first word (echo interims always
   do), and against any region for transcripts of several words.
3. Tail capture: the mic catches only the last word or two of a fully-played
   reply ("five" after "I'm Sonnet 5."). An exact word-suffix counts as echo,
   but only near the end of playback — earlier, those words haven't left the
   speaker yet, so a barge-in like "stop" can never be eaten by this rule.

Within the playback window, err toward eating: a leaked echo commits a phantom
turn and wastes an entire prompt-and-speak loop, while a swallowed real
utterance costs one repeat. The window is the safety — playback duration plus
a small margin — outside it nothing is ever suppressed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from difflib import SequenceMatcher

_MARGIN_S = 1.5  # slack past computed playback end: network + client buffering
_RUN = 5  # verbatim consecutive words that mark echo, not a human reply
_PREFIX_SIM = 0.6  # garbled-opening similarity; gated on an exact first word
_REGION_SIM = 0.67  # garbled-region similarity; corpus: echoes ≥.71, speech ≤.62
_REGION_MIN_WORDS = 4  # region matching needs enough signal to not misfire
_TAIL_WORDS = 2  # tail captures are a word or two, never a phrase
_TAIL_LAST_S = 1.0  # how close to playback end the tail rule arms

# TTS text writes digits; STT spells them out ("5" spoke as "five").
_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _norm(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower())
    return " ".join(_DIGITS.get(word, word) for word in cleaned.split())


def _similar(heard: str, region: str) -> float:
    return SequenceMatcher(None, heard, region).ratio()


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
        now = self._now()
        if now > playback_end + _MARGIN_S:
            return False
        words = _norm(text).split()
        spoken = self._spoken.split()
        if not words:
            return False

        if words == spoken[: len(words)]:  # verbatim opening (or the exact whole)
            return True

        heard_joined = "".join(words)
        spoken_joined = "".join(spoken)
        # An echo can't hold much more than was played; a real reply that
        # extends our words ("sounds good let's move on") is out of fuzzy scope.
        # Garbles run long ("I'm Sonnet 5." → "i'm connet 5 and five"), so 5/3.
        fits = 3 * len(heard_joined) <= 5 * len(spoken_joined)

        # Garbled opening: echo interims start at the reply's start, so the
        # first word comes through exact even when the rest is misheard.
        if fits and words[0] == spoken[0]:
            n = len(heard_joined)
            # Match the exact heard length, then a wider window: garble pads the
            # transcript longer than what we spoke.
            for width in (n, n + max(2, n // 4)):
                if _similar(heard_joined, spoken_joined[:width]) >= _PREFIX_SIM:
                    return True

        # Tail capture: the mic catching the reply's final words — which can
        # only happen once they have actually played.
        if (
            len(words) <= _TAIL_WORDS
            and now > playback_end - _TAIL_LAST_S
            and words == spoken[-len(words):]
        ):
            return True

        if len(words) >= _RUN:
            grams = {tuple(spoken[j : j + _RUN]) for j in range(len(spoken) - _RUN + 1)}
            if any(tuple(words[i : i + _RUN]) in grams for i in range(len(words) - _RUN + 1)):
                return True

        # Garbled region: enough heard words to make similarity meaningful.
        # seq2 is cached by SequenceMatcher; the quick_ratio upper bounds skip
        # ratio() for most offsets, keeping this cheap on the audio path.
        if fits and len(words) >= _REGION_MIN_WORDS:
            n = len(heard_joined)
            matcher = SequenceMatcher(None, "", heard_joined)
            for width in (n, n + max(2, n // 4)):
                for start in range(0, max(1, len(spoken_joined) - width + 1)):
                    matcher.set_seq1(spoken_joined[start : start + width])
                    if (
                        matcher.real_quick_ratio() >= _REGION_SIM
                        and matcher.quick_ratio() >= _REGION_SIM
                        and matcher.ratio() >= _REGION_SIM
                    ):
                        return True
        return False
