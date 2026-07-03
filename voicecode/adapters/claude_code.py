"""Claude Code implementation of ExecutionAdapter via the Claude Agent SDK.

One adapter drives at most one headless Claude Code session at a time. The SDK
client is built by `client_factory` so tests inject a fake; the default factory
(ClaudeSDKClient) spawns the bundled CLI subprocess. The client is connected
with no prompt — that puts the SDK in streaming mode, which the can_use_tool
permission callback requires — and user turns go in via client.query().

events() is single-consumer: one background pump distills SDK messages into
StatusEvents on an internal queue; a None sentinel ends the stream.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from voicecode.adapters.claude_code_distill import Distiller, describe_gate, describe_task
from voicecode.adapters.execution import ExecutionAdapter
from voicecode.config import Config
from voicecode.events import ErrorEvent, NeedsApproval, StatusEvent, TaskStarted

_SESSION_ID_TIMEOUT_S = 60.0


class ClaudeCodeAdapter(ExecutionAdapter):
    def __init__(
        self,
        config: Config,
        client_factory: Callable[[ClaudeAgentOptions], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or ClaudeSDKClient
        self._client: Any | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[StatusEvent | None] = asyncio.Queue()
        self._gates: dict[str, asyncio.Future[bool]] = {}
        self._distiller = Distiller()
        self._session_id: str | None = None
        self._session_ready = asyncio.Event()
        self._stream_ended = False

    async def start(self, prompt: str) -> str:
        await self._connect(resume=None)
        assert self._client is not None
        await self._client.query(prompt)
        self._emit(TaskStarted(summary=describe_task(prompt), detail=prompt))
        await asyncio.wait_for(self._session_ready.wait(), _SESSION_ID_TIMEOUT_S)
        if self._session_id is None:
            raise RuntimeError("execution session ended before reporting a session id")
        return self._session_id

    async def send(self, message: str) -> None:
        if self._client is None:
            raise RuntimeError("no active execution session")
        await self._client.query(message)
        self._emit(TaskStarted(summary=describe_task(message), detail=message))

    async def events(self) -> AsyncIterator[StatusEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def resume(self, session_id: str) -> None:
        await self._connect(resume=session_id)

    async def approve(self, gate_id: str, approved: bool) -> None:
        future = self._gates.get(gate_id)
        if future is not None and not future.done():
            future.set_result(approved)

    async def stop(self) -> None:
        client, self._client = self._client, None
        pump, self._pump_task = self._pump_task, None
        self._resolve_all_gates(approved=False)
        if pump is not None:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass  # teardown is best-effort; the stream is ending regardless
        self._end_stream()

    async def _connect(self, resume: str | None) -> None:
        if self._client is not None:
            raise RuntimeError("execution session already active; stop() it first")
        options = ClaudeAgentOptions(
            cwd=self.config.execution_cwd,
            resume=resume,
            can_use_tool=self._can_use_tool,
            system_prompt={"type": "preset", "preset": "claude_code"},
        )
        client = self._client_factory(options)
        await client.connect()
        self._client = client
        self._session_id = resume
        self._session_ready = asyncio.Event()
        self._distiller = Distiller()
        self._stream_ended = False
        while not self._queue.empty():  # drop leftovers from a previous session
            self._queue.get_nowait()
        self._pump_task = asyncio.create_task(self._pump(client))

    async def _pump(self, client: Any) -> None:
        try:
            async for message in client.receive_messages():
                self._on_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(
                ErrorEvent(
                    summary="The execution session hit an error.",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
        finally:
            self._session_ready.set()
            self._resolve_all_gates(approved=False)
            self._end_stream()

    def _on_message(self, message: Any) -> None:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                session_id = message.data.get("session_id")
                if session_id:
                    self._session_id = session_id
                self._session_ready.set()
            return
        if isinstance(message, AssistantMessage):
            for block in message.content:
                event: StatusEvent | None = None
                if isinstance(block, ToolUseBlock):
                    event = self._distiller.tool_use(block.name, block.input)
                elif isinstance(block, TextBlock):
                    event = self._distiller.assistant_text(block.text)
                if event is not None:
                    self._emit(event)
            return
        if isinstance(message, ResultMessage):
            self._emit(self._distiller.turn_result(message.result, message.is_error))

    async def _can_use_tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        gate_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._gates[gate_id] = future
        summary, detail = describe_gate(tool_name, tool_input, title=context.title)
        self._emit(
            NeedsApproval(gate_id=gate_id, tool_name=tool_name, summary=summary, detail=detail)
        )
        try:
            approved = await future
        finally:
            self._gates.pop(gate_id, None)
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by the user from voice-code.")

    def _resolve_all_gates(self, *, approved: bool) -> None:
        for future in self._gates.values():
            if not future.done():
                future.set_result(approved)
        self._gates.clear()

    def _emit(self, event: StatusEvent) -> None:
        if not self._stream_ended:
            self._queue.put_nowait(event)

    def _end_stream(self) -> None:
        if not self._stream_ended:
            self._stream_ended = True
            self._queue.put_nowait(None)
