import asyncio
import json
from pathlib import Path

import pytest

from server_fakes import FakeSubstrate
from remote_workstreams import protocol
from remote_workstreams.server.store import Store
from remote_workstreams.server.workstreams import WorkstreamManager

PLAN_TEXT = "Stint: Wire the auth flow\n\nGoal: ship it.\n"


def transcript_line(**fields) -> str:
    return json.dumps(fields) + "\n"


def append_transcript(path: Path, *lines: str) -> None:
    with path.open("a") as f:
        f.writelines(lines)


class Notify:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def __call__(self, message: object) -> None:
        self.messages.append(message)


@pytest.fixture
def rig(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    substrate = FakeSubstrate(tmp_path / "transcripts")
    notify = Notify()
    manager = WorkstreamManager(
        substrate,
        store,
        notify,
        convo_transcript=tmp_path / "convo.jsonl",
        data_dir=tmp_path / "data",
        plugin_dir=Path("/plugins/claude-code"),
        settings_file=tmp_path / "workstream-settings.json",
        poll_interval=0.01,
        poll_budget=1.0,
    )
    return manager, store, substrate, notify, tmp_path


def write_convo_lines(tmp_path, n: int) -> None:
    (tmp_path / "convo.jsonl").write_text("{}\n" * n)


def output_path(spec) -> Path:
    return Path(spec.initial_prompt.split("output=")[1])


async def wait_for_spawn(substrate, count=1):
    while len(substrate.spawned) < count:
        await asyncio.sleep(0.005)


async def launch(rig, plan_text=PLAN_TEXT):
    """Run new_workstream end-to-end, standing in for the planner; returns the
    plan path. The workstream session is substrate.spawned[-1]."""
    manager, store, substrate, notify, tmp_path = rig
    already = len(substrate.spawned)
    task = asyncio.create_task(manager.new_workstream())
    await wait_for_spawn(substrate, count=already + 1)
    plan = output_path(substrate.spawned[-1].spec)
    plan.write_text(plan_text)
    await task
    return plan


async def test_new_workstream_plans_then_launches(rig):
    manager, store, substrate, notify, tmp_path = rig
    write_convo_lines(tmp_path, 3)

    task = asyncio.create_task(manager.new_workstream())
    await wait_for_spawn(substrate)
    planner = substrate.spawned[0].spec
    assert (planner.name, planner.model, planner.effort) == ("plan", "opus", "high")
    assert planner.plugin_dir == Path("/plugins/claude-code")
    output = output_path(planner)
    assert planner.initial_prompt == (
        f"/remote-workstreams:role-stint-plan convo={tmp_path / 'convo.jsonl'}"
        f" since_line=0 output={output}"
    )
    assert output.parent == tmp_path / "data" / "plans"
    assert store.get_marker() == 3  # current line count recorded at plan time

    output.write_text(PLAN_TEXT)
    await task
    assert substrate.killed == ["voice:plan"]

    # The plan launched straight into a workstream — no review step, no plan push.
    session = substrate.spawned[1]
    spec = session.spec
    assert spec.name == "ws-wire-the-auth-flow"
    assert (spec.model, spec.effort) == ("fable", "xhigh")
    assert spec.settings_file == tmp_path / "workstream-settings.json"
    assert spec.initial_prompt == "/role-root"
    assert spec.display_name == "Wire the auth flow"
    # the full plan text is pasted as the first message
    assert substrate.sent == [("voice:ws-wire-the-auth-flow", PLAN_TEXT)]

    (row,) = store.list_workstreams()
    assert row.name == "ws-wire-the-auth-flow"
    assert row.cc_session_id == session.session_id
    assert row.plan_path == str(output)
    assert row.status == "running"

    (cards,) = notify.messages
    assert cards.type == "workstreams"
    assert (cards.convo_model, cards.workstream_model) == ("fable", "fable")  # defaults
    (card,) = cards.workstreams
    assert (card.name, card.title, card.status, card.model) == (
        "ws-wire-the-auth-flow", "Wire the auth flow", "running", "fable",
    )


async def test_new_workstream_since_line_comes_from_stored_marker(rig):
    manager, store, substrate, notify, tmp_path = rig
    store.set_marker(7)
    write_convo_lines(tmp_path, 9)
    task = asyncio.create_task(manager.new_workstream())
    await wait_for_spawn(substrate)
    assert "since_line=7" in substrate.spawned[0].spec.initial_prompt
    assert store.get_marker() == 9
    output_path(substrate.spawned[0].spec).write_text(PLAN_TEXT)
    await task


async def test_new_workstream_waits_out_empty_plan_file(rig):
    """The planner's output file can exist before its content lands; an empty
    read must not be treated as the plan."""
    manager, store, substrate, notify, tmp_path = rig
    task = asyncio.create_task(manager.new_workstream())
    await wait_for_spawn(substrate)
    output = output_path(substrate.spawned[0].spec)
    output.write_text("")  # created, not yet written
    await asyncio.sleep(0.05)
    output.write_text(PLAN_TEXT)
    await task
    (card,) = notify.messages[-1].workstreams
    assert card.name == "ws-wire-the-auth-flow"


async def test_new_workstream_planner_timeout_pushes_error(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager._poll_budget = 0.03
    await manager.new_workstream()  # nobody writes the output file
    assert substrate.killed == ["voice:plan"]
    assert len(substrate.spawned) == 1  # no workstream was launched
    (error,) = notify.messages
    assert error.type == "error"
    assert "timed out" in error.message


async def test_send_to_workstream_ferries_directive_and_advances_marker(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    ws_session = substrate.spawned[-1]
    store.set_marker(2)
    write_convo_lines(tmp_path, 5)

    task = asyncio.create_task(manager.send_to_workstream("ws-wire-the-auth-flow"))
    await wait_for_spawn(substrate, count=3)
    spec = substrate.spawned[2].spec
    assert (spec.name, spec.model, spec.effort) == ("inject", "opus", "high")
    output = output_path(spec)
    assert spec.initial_prompt == (
        f"/remote-workstreams:role-inject convo={tmp_path / 'convo.jsonl'} since_line=2"
        f" workstream={ws_session.transcript} output={output}"
    )
    output.write_text("Focus the retry loop on idempotent writes only.")
    await task
    assert substrate.killed[-1] == "voice:inject"
    assert substrate.sent[-1] == (
        "voice:ws-wire-the-auth-flow",
        "Focus the retry loop on idempotent writes only.",
    )
    assert store.get_marker() == 5  # advanced after the paste


async def test_send_to_workstream_timeout_keeps_marker(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    notify.messages.clear()
    store.set_marker(2)
    write_convo_lines(tmp_path, 5)
    manager._poll_budget = 0.03
    await manager.send_to_workstream("ws-wire-the-auth-flow")
    (error,) = notify.messages
    assert error.type == "error" and "timed out" in error.message
    assert substrate.killed[-1] == "voice:inject"
    assert store.get_marker() == 2  # nothing was delivered; the delta is not lost


async def test_send_to_unknown_workstream_pushes_error(rig):
    manager, store, substrate, notify, tmp_path = rig
    await manager.send_to_workstream("ws-nope")
    (error,) = notify.messages
    assert error.type == "error" and "unknown workstream" in error.message


async def test_cards_track_window_aliveness(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[-1]

    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert (card.name, card.title, card.status) == (
        "ws-wire-the-auth-flow", "Wire the auth flow", "running",
    )

    substrate.alive_windows.discard(session.window)  # the window died
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert card.status == "gone"
    assert store.list_workstreams()[0].status == "gone"


async def test_cards_carry_vitals_from_the_transcript(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[-1]

    # The fake's greeting has no turn_duration yet: the session is mid-turn.
    append_transcript(
        session.transcript,
        transcript_line(
            type="assistant",
            message={
                "usage": {"input_tokens": 2, "cache_read_input_tokens": 399_998},
                "content": [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}],
            },
        ),
    )
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert (card.state, card.agents, card.context_pct) == ("thinking", 1, 40)

    append_transcript(
        session.transcript,
        transcript_line(
            type="user",
            message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
        ),
        transcript_line(type="system", subtype="turn_duration", durationMs=1),
    )
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert (card.state, card.agents) == ("waiting", 0)


async def test_push_carries_convo_context_pct_and_follows_repointing(rig):
    manager, store, substrate, notify, tmp_path = rig
    (tmp_path / "convo.jsonl").write_text(
        transcript_line(type="assistant", message={"usage": {"input_tokens": 100_000}, "content": []})
    )
    await manager.push_cards()
    assert notify.messages[-1].convo_context_pct == 10

    fresh = tmp_path / "convo-fresh.jsonl"  # Clear swaps the convo session
    fresh.write_text(
        transcript_line(type="assistant", message={"usage": {"input_tokens": 10_000}, "content": []})
    )
    manager.convo_transcript = fresh
    await manager.push_cards()
    assert notify.messages[-1].convo_context_pct == 1


async def test_run_pushes_even_with_no_workstreams(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager._push_interval = 0.02
    task = asyncio.create_task(manager.run())
    while not notify.messages:
        await asyncio.sleep(0.005)
    task.cancel()
    assert notify.messages[-1].workstreams == []


async def test_compact_workstream_types_slash_compact(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    await manager.compact_workstream("ws-wire-the-auth-flow")
    assert substrate.sent[-1] == ("voice:ws-wire-the-auth-flow", "/compact")

    await manager.compact_workstream("ws-nope")
    error = notify.messages[-1]
    assert error.type == "error" and "unknown workstream" in error.message


async def test_set_model_shapes_new_spawns_not_running_ones(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)  # launched before the pick: fable
    manager.set_model("workstream", "opus")

    await launch(rig, plan_text="Stint: Write the docs\n\nGoal: docs.\n")
    spec = substrate.spawned[-1].spec
    assert (spec.model, spec.effort) == ("opus", "xhigh")  # effort is fixed per role

    await manager.push_cards()
    first, second = notify.messages[-1].workstreams
    assert (first.model, second.model) == ("fable", "opus")  # the old card keeps its model
    assert notify.messages[-1].workstream_model == "opus"


async def test_codex_pick_launches_a_codex_workstream(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager.set_model("workstream", "luna")
    await launch(rig)

    planner, session = substrate.spawned
    assert planner.spec.engine == "claude"  # the planner roster is fixed regardless
    spec = session.spec
    assert (spec.engine, spec.model, spec.effort) == ("codex", "luna", "xhigh")
    assert spec.initial_prompt == "$role-root"  # codex skill invocation
    assert spec.settings_file is None  # the approval relay hook is claude-only

    (row,) = store.list_workstreams()
    assert (row.model, row.engine) == ("luna", "codex")
    (card,) = notify.messages[-1].workstreams
    assert (card.model, card.engine) == ("luna", "codex")

    manager2 = WorkstreamManager(  # engine survives a restart via the row
        substrate,
        store,
        notify,
        convo_transcript=tmp_path / "convo.jsonl",
        data_dir=tmp_path / "data",
        plugin_dir=Path("/plugins/claude-code"),
        settings_file=tmp_path / "workstream-settings.json",
    )
    notify.messages.clear()
    await manager2.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert (card.model, card.engine) == ("luna", "codex")


async def test_codex_cards_read_vitals_from_the_rollout(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager.set_model("workstream", "luna")
    await launch(rig)
    session = substrate.spawned[-1]

    append_transcript(
        session.transcript,
        transcript_line(
            type="event_msg",
            payload={"type": "task_started", "turn_id": "t1", "model_context_window": 200_000},
        ),
        transcript_line(
            type="event_msg",
            payload={
                "type": "token_count",
                "info": {"last_token_usage": {"total_tokens": 100_000},
                         "model_context_window": 200_000},
            },
        ),
    )
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert (card.state, card.agents, card.context_pct) == ("thinking", 0, 50)

    append_transcript(
        session.transcript,
        transcript_line(type="event_msg", payload={"type": "task_complete", "turn_id": "t1"}),
    )
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert card.state == "waiting"


async def test_manager_rehydrates_from_store(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[-1]

    manager2 = WorkstreamManager(
        substrate,
        store,
        notify,
        convo_transcript=tmp_path / "convo.jsonl",
        data_dir=tmp_path / "data",
        plugin_dir=Path("/plugins/claude-code"),
        settings_file=tmp_path / "workstream-settings.json",
    )
    assert manager2.transcript_path("ws-wire-the-auth-flow") == session.transcript
    notify.messages.clear()
    await manager2.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert card.name == "ws-wire-the-auth-flow" and card.status == "running"
    assert card.model == "fable"  # per-row model survives the restart


async def test_run_pushes_cards_on_interval(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager._push_interval = 0.02
    await launch(rig)
    notify.messages.clear()
    task = asyncio.create_task(manager.run())
    while len(notify.messages) < 2:
        await asyncio.sleep(0.005)
    task.cancel()
    assert all(isinstance(m, protocol.Workstreams) for m in notify.messages)


async def test_end_workstream_kills_window_and_drops_card(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[-1]

    await manager.end_workstream("ws-wire-the-auth-flow")
    assert session.window in substrate.killed
    assert store.list_workstreams() == []
    assert notify.messages[-1].workstreams == []

    await manager.end_workstream("ws-wire-the-auth-flow")  # already gone
    assert isinstance(notify.messages[-1], protocol.Error)
