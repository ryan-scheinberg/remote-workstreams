"""ClaudeCodeAdapter tests. No real Claude Code session, no live API — the SDK
client is replaced by FakeClient via the adapter's client_factory seam. SDK
message dataclasses are the real ones from claude_agent_sdk."""

import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from voicecode.adapters.claude_code import ClaudeCodeAdapter
from voicecode.config import Config
from voicecode.events import (
    Completed,
    ErrorEvent,
    Finding,
    NeedsApproval,
    Progress,
    TaskStarted,
)


class FakeClient:
    """Stands in for ClaudeSDKClient. Tests feed SDK messages into `incoming`;
    an Exception item is raised from receive_messages; None ends the stream."""

    def __init__(self, options):
        self.options = options
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.connected = False
        self.disconnected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self.sent.append(prompt)

    async def receive_messages(self):
        while True:
            item = await self.incoming.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def disconnect(self):
        self.disconnected = True


def make_adapter(preload_init=True, end_immediately=False):
    clients: list[FakeClient] = []

    def factory(options):
        client = FakeClient(options)
        if preload_init:
            client.incoming.put_nowait(
                SystemMessage(subtype="init", data={"session_id": "sess-1"})
            )
        if end_immediately:
            client.incoming.put_nowait(None)
        clients.append(client)
        return client

    return ClaudeCodeAdapter(Config(), client_factory=factory), clients


def result_message(result=None, is_error=False):
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        result=result,
    )


async def next_event(stream, timeout=2.0):
    return await asyncio.wait_for(anext(stream), timeout)


async def _drain(stream):
    return [event async for event in stream]


async def drain(stream, timeout=2.0):
    return await asyncio.wait_for(_drain(stream), timeout)


async def test_start_returns_session_id_and_configures_sdk():
    adapter, clients = make_adapter()
    session_id = await adapter.start("fix the login bug")
    assert session_id == "sess-1"
    client = clients[0]
    assert client.connected
    assert client.sent == ["fix the login bug"]
    assert client.options.cwd == Config().execution_cwd
    assert client.options.resume is None
    assert client.options.can_use_tool is not None
    first = await next_event(adapter.events())
    assert isinstance(first, TaskStarted)
    assert first.summary == "Starting on: fix the login bug."
    assert first.detail == "fix the login bug"


async def test_start_twice_raises():
    adapter, _ = make_adapter()
    await adapter.start("a")
    with pytest.raises(RuntimeError):
        await adapter.start("b")


async def test_start_raises_if_stream_ends_before_init():
    adapter, _ = make_adapter(preload_init=False, end_immediately=True)
    with pytest.raises(RuntimeError):
        await adapter.start("go")


async def test_send_before_start_raises():
    adapter, _ = make_adapter()
    with pytest.raises(RuntimeError):
        await adapter.send("hello")


async def test_send_delivers_message_and_marks_new_turn():
    adapter, clients = make_adapter()
    await adapter.start("first task")
    await adapter.send("also check the tests")
    assert clients[0].sent == ["first task", "also check the tests"]
    stream = adapter.events()
    assert isinstance(await next_event(stream), TaskStarted)
    follow_up = await next_event(stream)
    assert isinstance(follow_up, TaskStarted)
    assert follow_up.detail == "also check the tests"


async def test_sdk_traffic_distilled_into_events():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    client.incoming.put_nowait(
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "uv run pytest"})],
            model="m",
        )
    )
    text = (
        "The tests fail because the fixture database is missing a migration "
        "for the sessions table."
    )
    client.incoming.put_nowait(AssistantMessage(content=[TextBlock(text=text)], model="m"))
    client.incoming.put_nowait(result_message(result="Fixed. All tests green."))
    stream = adapter.events()
    started, progress, finding, completed = [await next_event(stream) for _ in range(4)]
    assert isinstance(started, TaskStarted)
    assert isinstance(progress, Progress)
    assert progress.summary == "Running the test suite."
    assert progress.detail == "uv run pytest"
    assert isinstance(finding, Finding)
    assert finding.detail == text
    assert isinstance(completed, Completed)
    assert completed.summary == "Fixed."
    assert completed.detail == "Fixed. All tests green."


async def test_repeated_tool_calls_debounced():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    for path in ("/a.py", "/b.py", "/c.py"):
        client.incoming.put_nowait(
            AssistantMessage(
                content=[ToolUseBlock(id=path, name="Read", input={"file_path": path})],
                model="m",
            )
        )
    client.incoming.put_nowait(
        AssistantMessage(
            content=[ToolUseBlock(id="t9", name="Bash", input={"command": "ls"})], model="m"
        )
    )
    client.incoming.put_nowait(result_message())
    stream = adapter.events()
    events = [await next_event(stream) for _ in range(4)]
    assert [type(e) for e in events] == [TaskStarted, Progress, Progress, Completed]
    assert events[1].summary == "Reading a.py."
    assert events[2].summary == "Running ls."


async def test_error_result_becomes_error_event():
    adapter, clients = make_adapter()
    await adapter.start("go")
    clients[0].incoming.put_nowait(
        result_message(result="Max budget exceeded before finishing.", is_error=True)
    )
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    error = await next_event(stream)
    assert isinstance(error, ErrorEvent)
    assert error.summary == "Max budget exceeded before finishing."


async def test_gate_allow_roundtrip():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    call = asyncio.create_task(
        client.options.can_use_tool("Bash", {"command": "rm -rf build"}, ToolPermissionContext())
    )
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    gate = await next_event(stream)
    assert isinstance(gate, NeedsApproval)
    assert gate.tool_name == "Bash"
    assert gate.summary.startswith("Approval needed:")
    assert gate.detail == "rm -rf build"
    assert not call.done()
    await adapter.approve(gate.gate_id, True)
    result = await asyncio.wait_for(call, 2)
    assert isinstance(result, PermissionResultAllow)


async def test_gate_deny_roundtrip():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    call = asyncio.create_task(
        client.options.can_use_tool("Write", {"file_path": "/etc/hosts"}, ToolPermissionContext())
    )
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    gate = await next_event(stream)
    await adapter.approve(gate.gate_id, False)
    result = await asyncio.wait_for(call, 2)
    assert isinstance(result, PermissionResultDeny)
    assert result.message


async def test_two_pending_gates_resolve_independently():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    ctx = ToolPermissionContext()
    first = asyncio.create_task(
        client.options.can_use_tool("Bash", {"command": "rm -rf build"}, ctx)
    )
    second = asyncio.create_task(client.options.can_use_tool("Write", {"file_path": "/x"}, ctx))
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    gate_one = await next_event(stream)
    gate_two = await next_event(stream)
    assert gate_one.gate_id != gate_two.gate_id
    # Resolve out of order — neither blocks the other.
    await adapter.approve(gate_two.gate_id, False)
    await adapter.approve(gate_one.gate_id, True)
    assert isinstance(await asyncio.wait_for(first, 2), PermissionResultAllow)
    assert isinstance(await asyncio.wait_for(second, 2), PermissionResultDeny)


async def test_unknown_gate_id_is_noop():
    adapter, _ = make_adapter()
    await adapter.approve("no-such-gate", True)


async def test_stop_tears_down_and_ends_stream():
    adapter, clients = make_adapter()
    await adapter.start("go")
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    await adapter.stop()
    assert clients[0].disconnected
    assert await drain(stream) == []


async def test_stop_denies_pending_gate():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    call = asyncio.create_task(
        client.options.can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())
    )
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    assert isinstance(await next_event(stream), NeedsApproval)
    await adapter.stop()
    assert isinstance(await asyncio.wait_for(call, 2), PermissionResultDeny)


async def test_natural_stream_end_ends_events_and_denies_gates():
    adapter, clients = make_adapter()
    await adapter.start("go")
    client = clients[0]
    call = asyncio.create_task(
        client.options.can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())
    )
    stream = adapter.events()
    await next_event(stream)  # TaskStarted
    assert isinstance(await next_event(stream), NeedsApproval)
    client.incoming.put_nowait(None)  # CLI stream closes underneath us
    assert await drain(stream) == []
    assert isinstance(await asyncio.wait_for(call, 2), PermissionResultDeny)


async def test_pump_exception_becomes_error_event_and_stream_never_raises():
    adapter, clients = make_adapter()
    await adapter.start("go")
    clients[0].incoming.put_nowait(RuntimeError("transport died"))
    events = await drain(adapter.events())
    assert [type(e) for e in events] == [TaskStarted, ErrorEvent]
    assert "transport died" in (events[1].detail or "")


async def test_resume_passes_session_id_to_sdk():
    adapter, clients = make_adapter()
    await adapter.resume("prior-99")
    client = clients[0]
    assert client.connected
    assert client.options.resume == "prior-99"
    await adapter.send("continue where you left off")
    assert client.sent == ["continue where you left off"]


async def test_restart_after_stop_uses_fresh_session():
    adapter, clients = make_adapter()
    await adapter.start("first")
    await adapter.stop()
    session_id = await adapter.start("second")
    assert session_id == "sess-1"
    assert len(clients) == 2
    first = await next_event(adapter.events())
    assert isinstance(first, TaskStarted)
    assert first.detail == "second"
