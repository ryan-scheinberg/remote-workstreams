"""Pending phone approvals. POST /approvals parks on a future here; the phone's
WS approval message resolves it. No verdict within the timeout → TimeoutError →
the API answers 408 and the relay hook stays silent (Claude Code's native
permission behavior takes over).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from voicecode import protocol

Notify = Callable[[object], Awaitable[None]]


class Approvals:
    def __init__(self, notify: Notify, timeout: float = 60.0) -> None:
        self._notify = notify
        self.timeout = timeout
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def create(self, session: str, tool: str, summary: str) -> bool:
        """Push an approval card to the phone; block until resolve() or timeout."""
        approval_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = future
        await self._notify(
            protocol.ApprovalRequest(
                approval_id=approval_id, session=session, tool=tool, summary=summary
            )
        )
        try:
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> None:
        future = self.pending.get(approval_id)
        if future is not None and not future.done():
            future.set_result(approved)
