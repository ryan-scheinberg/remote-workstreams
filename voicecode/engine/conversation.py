"""The conversation agent — a raw Anthropic streaming loop, NOT a Claude Code session.

STUB — replaced by the engine unit. Class name, module path, and method signatures
are frozen; the server and audio pipeline code against them.

Non-negotiables (see PROJECT_BRIEF.md):
- Frozen system prompt with a cache_control breakpoint; every dynamic thing goes
  after it. Prompt caching is a latency requirement, not an optimization.
- Status events inject as a <system-reminder> block inside the next user turn.
- Coherence rule: speak only to events actually received; defer naturally on
  in-flight work; never fabricate results.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from anthropic import AsyncAnthropic

from voicecode.events import StatusEvent


class ConversationEngine:
    def __init__(self, client: AsyncAnthropic, model: str = "claude-haiku-4-5") -> None:
        self.client = client
        self.model = model
        self.messages: list[dict[str, Any]] = []  # owned message list — the bridge depends on it

    def inject_events(self, events: Sequence[StatusEvent]) -> None:
        """Queue events for injection into the next turn."""
        raise NotImplementedError

    def turn(self, user_text: str) -> AsyncIterator[str]:
        """Run one user turn; yield sentence chunks sized for streaming TTS."""
        raise NotImplementedError

    def proactive_turn(self) -> AsyncIterator[str]:
        """Unsolicited speech for queued completed/needs_approval events while the
        user is silent. Yields nothing when there is nothing worth saying."""
        raise NotImplementedError

    def export_messages(self) -> list[dict[str, Any]]:
        """Snapshot for session persistence (the server owns storage)."""
        raise NotImplementedError

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        """Restore a persisted conversation for resume."""
        raise NotImplementedError
