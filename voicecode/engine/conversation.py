"""The conversation agent — a raw Anthropic streaming loop, NOT a Claude Code session.

Class name, module path, and method signatures are frozen; the server and audio
pipeline code against them.

Non-negotiables (see PROJECT_BRIEF.md):
- Frozen system prompt with a cache_control breakpoint; every dynamic thing goes
  after it. Prompt caching is a latency requirement, not an optimization.
- Status events inject as a <system-reminder> block inside the next user turn.
- Coherence rule: speak only to events actually received; defer naturally on
  in-flight work; never fabricate results.
- Dispatch: the agent asks for execution work by embedding
  <dispatch>concise directive</dispatch> at the end of its raw reply (prompt
  convention — no tools). The engine strips it from spoken chunks and exposes
  it via take_dispatch().
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from anthropic import AsyncAnthropic

from voicecode.engine.chunker import SentenceChunker
from voicecode.engine.dispatch import DispatchFilter
from voicecode.engine.prompt import PROACTIVE_NOTE, render_events, system_blocks
from voicecode.events import StatusEvent

_MAX_TOKENS = 1024  # spoken replies are short; headroom for the dispatch tag

_PROACTIVE_TYPES = ("completed", "needs_approval")


class ConversationEngine:
    def __init__(self, client: AsyncAnthropic, model: str = "claude-haiku-4-5") -> None:
        self.client = client
        self.model = model
        self.messages: list[dict[str, Any]] = []  # owned message list — the bridge depends on it
        self._pending: list[StatusEvent] = []
        self._dispatch: str | None = None

    def inject_events(self, events: Sequence[StatusEvent]) -> None:
        """Queue events for injection into the next turn."""
        self._pending.extend(events)

    async def turn(self, user_text: str) -> AsyncIterator[str]:
        """Run one user turn; yield sentence chunks sized for streaming TTS."""
        events = self._drain_events()
        content = f"{render_events(events)}\n\n{user_text}" if events else user_text
        self.messages.append({"role": "user", "content": content})
        async for chunk in self._stream_reply():
            yield chunk

    async def proactive_turn(self) -> AsyncIterator[str]:
        """Unsolicited speech for queued completed/needs_approval events while the
        user is silent. Yields nothing when there is nothing worth saying."""
        if not any(e.type in _PROACTIVE_TYPES for e in self._pending):
            return  # progress/finding events ride along in the next real turn
        events = self._drain_events()
        content = f"{render_events(events)}\n\n{PROACTIVE_NOTE}"
        self.messages.append({"role": "user", "content": content})
        async for chunk in self._stream_reply():
            yield chunk

    def take_dispatch(self) -> str | None:
        """Directive the last turn asked to send to the execution layer, if any.
        Returns it once and clears it; the caller routes it to the ExecutionAdapter."""
        dispatch = self._dispatch
        self._dispatch = None
        return dispatch

    def export_messages(self) -> list[dict[str, Any]]:
        """Snapshot for session persistence (the server owns storage)."""
        return [dict(m) for m in self.messages]

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        """Restore a persisted conversation for resume."""
        self.messages = [dict(m) for m in messages]

    def _drain_events(self) -> list[StatusEvent]:
        events = self._pending
        self._pending = []
        return events

    async def _stream_reply(self) -> AsyncIterator[str]:
        """Stream one assistant reply: strip dispatch tags, chunk into sentences,
        and append the raw reply to the owned message list."""
        fltr = DispatchFilter()
        chunker = SentenceChunker()
        raw: list[str] = []
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            system=system_blocks(),
            messages=self.messages,
        ) as stream:
            async for delta in stream.text_stream:
                raw.append(delta)
                for chunk in chunker.feed(fltr.feed(delta)):
                    yield chunk
        for chunk in chunker.feed(fltr.flush()):
            yield chunk
        if tail := chunker.flush():
            yield tail
        if fltr.dispatch:
            self._dispatch = fltr.dispatch
        if reply := "".join(raw).strip():
            # History keeps the raw reply (dispatch tag included) so later turns
            # remember what was handed to the execution layer.
            self.messages.append({"role": "assistant", "content": reply})
