"""Claude Code implementation of ExecutionAdapter via the Claude Agent SDK.

STUB — replaced by the execution unit. The class name and module path are frozen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from voicecode.adapters.execution import ExecutionAdapter
from voicecode.config import Config
from voicecode.events import StatusEvent


class ClaudeCodeAdapter(ExecutionAdapter):
    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, prompt: str) -> str:
        raise NotImplementedError

    async def send(self, message: str) -> None:
        raise NotImplementedError

    def events(self) -> AsyncIterator[StatusEvent]:
        raise NotImplementedError

    async def resume(self, session_id: str) -> None:
        raise NotImplementedError

    async def approve(self, gate_id: str, approved: bool) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError
