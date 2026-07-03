"""Strip <dispatch>…</dispatch> tags from a text stream and capture the directive.

The model appends the tag at the end of its raw reply (a prompt convention, not a
tool). Deltas can split the tag anywhere — "<dis" + "patch>fix it</disp" + "atch>" —
so anything that could still become a tag is buffered until it resolves either way.
"""

from __future__ import annotations

_OPEN = "<dispatch>"
_CLOSE = "</dispatch>"


class DispatchFilter:
    def __init__(self) -> None:
        self._buf = ""  # possible partial tag, carried across deltas
        self._in_dispatch = False
        self._captured: list[str] = []
        self.dispatch: str | None = None

    def feed(self, text: str) -> str:
        """Consume a stream delta; return the speakable portion."""
        work = self._buf + text
        self._buf = ""
        spoken: list[str] = []
        while work:
            tag = _CLOSE if self._in_dispatch else _OPEN
            i = work.find("<")
            if i == -1:
                self._emit(spoken, work)
                break
            self._emit(spoken, work[:i])
            rest = work[i:]
            if rest.startswith(tag):
                if self._in_dispatch:
                    self.dispatch = "".join(self._captured).strip() or None
                    self._captured = []
                self._in_dispatch = not self._in_dispatch
                work = rest[len(tag) :]
            elif tag.startswith(rest):
                self._buf = rest  # could still become the tag — wait for more
                break
            else:
                self._emit(spoken, "<")
                work = rest[1:]
        return "".join(spoken)

    def flush(self) -> str:
        """End of stream: resolve any buffered partial tag; return leftover speech."""
        if self._in_dispatch:
            # Stream ended inside a dispatch tag (possibly mid-close-tag, which is
            # what _buf would hold). The content is a directive either way.
            self.dispatch = "".join(self._captured).strip() or self.dispatch
            self._captured = []
            self._buf = ""
            return ""
        leftover = self._buf  # a "<disp"-style prefix that never became a tag
        self._buf = ""
        return leftover

    def _emit(self, spoken: list[str], text: str) -> None:
        if not text:
            return
        (self._captured if self._in_dispatch else spoken).append(text)
