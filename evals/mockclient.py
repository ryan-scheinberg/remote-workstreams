"""Fake and recording clients for the bridge evals.

MockClient stands in for AsyncAnthropic: each `messages.stream()` call pops the
next scripted reply and streams it in small deltas (so dispatch tags and sentence
boundaries get split across deltas, exercising the real stream paths).

RecordingClient wraps a real AsyncAnthropic for --live runs so prompt-side
assertions can see the request kwargs either way.
"""

from __future__ import annotations

import copy
from typing import Any

_DELTA_SIZE = 7  # small enough to split tags/sentences across deltas


class _FakeTextStream:
    def __init__(self, text: str) -> None:
        self._deltas = [text[i : i + _DELTA_SIZE] for i in range(0, len(text), _DELTA_SIZE)]

    def __aiter__(self) -> "_FakeTextStream":
        return self

    async def __anext__(self) -> str:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)


class _FakeStream:
    def __init__(self, text: str) -> None:
        self.text_stream = _FakeTextStream(text)


class _FakeStreamManager:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self) -> _FakeStream:
        return _FakeStream(self._text)

    async def __aexit__(self, *exc: object) -> None:
        return None


class _MockMessages:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.calls.append(copy.deepcopy(kwargs))  # engine mutates messages after the call
        if not self.replies:
            raise AssertionError("MockClient ran out of scripted replies")
        return _FakeStreamManager(self.replies.pop(0))


class MockClient:
    """Duck-typed AsyncAnthropic replacement: scripted replies, recorded calls."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.messages = _MockMessages()
        self.messages.replies = list(replies or [])

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


class _RecordingMessages:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        return self._inner.stream(**kwargs)


class RecordingClient:
    """Wraps a real AsyncAnthropic so evals can assert on outgoing requests."""

    def __init__(self, inner: Any) -> None:
        self.messages = _RecordingMessages(inner.messages)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls
