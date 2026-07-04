"""Incremental sentence chunking for streaming TTS.

Feed text in as it arrives (transcript text blocks, stream deltas); complete
sentences come out as soon as their boundary arrives, so the first chunk
reaches TTS at the first sentence boundary instead of at end-of-text.
Sentences shorter than `min_chars` are merged with the next one so TTS isn't
fed choppy fragments like "Sure.".
"""

from __future__ import annotations

import re

# A sentence ends at . ! ? or …, optionally followed by closing quotes/brackets,
# only when whitespace follows (so "3.14" never splits). A bare newline is also
# a boundary. The whitespace must already be in the buffer — a terminator at the
# buffer's edge may still be mid-token (more deltas coming); flush() covers EOF.
_BOUNDARY = re.compile(r"[.!?…]['\")\]]*(?=\s)|\n")

_MIN_CHARS = 20


class SentenceChunker:
    def __init__(self, min_chars: int = _MIN_CHARS) -> None:
        self.min_chars = min_chars
        self._buf = ""
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        """Consume a stream delta; return any chunks now ready for TTS."""
        self._buf += text
        out: list[str] = []
        while match := _BOUNDARY.search(self._buf):
            sentence = self._buf[: match.end()].strip()
            self._buf = self._buf[match.end() :].lstrip()
            if not sentence:
                continue
            self._pending = f"{self._pending} {sentence}" if self._pending else sentence
            if len(self._pending) >= self.min_chars:
                out.append(self._pending)
                self._pending = ""
        return out

    def flush(self) -> str | None:
        """Return whatever remains at end of stream, if anything."""
        tail = self._buf.strip()
        chunk = f"{self._pending} {tail}".strip() if self._pending else tail
        self._buf = ""
        self._pending = ""
        return chunk or None
