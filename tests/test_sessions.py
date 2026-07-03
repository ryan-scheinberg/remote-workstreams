import asyncio

import pytest

from server_fakes import FakeConn, Fakes
from voicecode.events import Finding
from voicecode.protocol import Event
from voicecode.server.sessions import SessionManager, UnknownSession
from voicecode.server.store import Store


def make_manager(tmp_path) -> tuple[Store, Fakes, SessionManager]:
    store = Store(tmp_path / "db.sqlite3")
    fakes = Fakes()
    manager = SessionManager(
        store,
        engine_factory=fakes.engine_factory,
        execution_factory=fakes.execution_factory,
        stt_factory=fakes.stt_factory,
        tts_factory=fakes.tts_factory,
        pipeline_factory=fakes.pipeline_factory,
    )
    return store, fakes, manager


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_events_buffer_while_detached_and_flush_on_attach(tmp_path):
    store, fakes, manager = make_manager(tmp_path)
    runtime = await manager.attach(None, FakeConn())
    await runtime.pipeline.on_dispatch("work")
    await settle()

    await manager.detach(runtime)
    assert runtime.pipeline is None and runtime.conn is None

    fakes.executions[0].push_event(Finding(summary="The bug is in the retry loop."))
    await settle()
    assert [e.type for e in runtime.pending_events] == ["finding"]

    conn = FakeConn()
    again = await manager.attach(None, conn)
    assert again is runtime  # the runtime survived the disconnect
    assert runtime.pending_events == []
    assert [e.type for e in fakes.pipelines[-1].events] == ["finding"]
    event_messages = [m for m in conn.messages if isinstance(m, Event)]
    assert [m.event.type for m in event_messages] == ["finding"]
    await manager.shutdown()


async def test_detach_persists_engine_messages(tmp_path):
    store, fakes, manager = make_manager(tmp_path)
    runtime = await manager.attach(None, FakeConn())
    runtime.engine.messages = [{"role": "user", "content": "hi"}]
    await manager.detach(runtime)
    assert store.get_session(runtime.session_id).messages == [{"role": "user", "content": "hi"}]
    await manager.shutdown()


async def test_attach_unknown_session_raises(tmp_path):
    _, _, manager = make_manager(tmp_path)
    with pytest.raises(UnknownSession):
        await manager.attach("missing", FakeConn())


async def test_takeover_closes_old_connection_across_sessions(tmp_path):
    store, fakes, manager = make_manager(tmp_path)
    old_conn = FakeConn()
    await manager.attach(None, old_conn)
    other = store.create_session("Other")

    new_conn = FakeConn()
    runtime = await manager.attach(other.id, new_conn)  # different session, still takes over
    assert runtime.session_id == other.id
    assert old_conn.closed_error == "another connection took over"
    assert manager.live is runtime
    await manager.shutdown()


async def test_shutdown_stops_execution(tmp_path):
    _, fakes, manager = make_manager(tmp_path)
    runtime = await manager.attach(None, FakeConn())
    await runtime.pipeline.on_dispatch("work")
    await settle()
    await manager.shutdown()
    assert fakes.executions[0].stopped
    assert runtime.pump_task.done()
