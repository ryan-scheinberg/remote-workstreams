"""ExecutionAdapter — a headless coding agent behind a uniform interface.

Claude Code (Agent SDK) is the v1 implementation; the interface is designed so a
Codex implementation can land later without touching the engine or server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from voicecode.events import StatusEvent


class ExecutionAdapter(ABC):
    @abstractmethod
    async def start(self, prompt: str) -> str:
        """Start a fresh execution session; returns its session id."""

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send a follow-up message into the running session."""

    @abstractmethod
    def events(self) -> AsyncIterator[StatusEvent]:
        """Distilled activity stream. Yields until the session ends; never raises —
        failures surface as ErrorEvent."""

    @abstractmethod
    async def resume(self, session_id: str) -> None:
        """Attach to a previous execution session."""

    @abstractmethod
    async def approve(self, gate_id: str, approved: bool) -> None:
        """Resolve a NeedsApproval gate; the verdict flows into the permission callback."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the session and end the events stream."""
