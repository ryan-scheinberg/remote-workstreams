import asyncio
import json
from pathlib import Path

import pytest

from server_fakes import FakeSubstrate
from voicecode import protocol
from voicecode.server.store import Store
from voicecode.server.workstreams import WorkstreamManager

PLAN_TEXT = "Stint: Wire the auth flow\n\nGoal: ship it.\n"


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


async def launch(rig, plan_id="abc12345", plan_text=PLAN_TEXT):
    manager, store, substrate, notify, tmp_path = rig
    plan = tmp_path / "data" / "plans" / f"plan-{plan_id}.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(plan_text)
    await manager.launch_workstream(plan_id)
    return plan


async def test_plan_stint_spawns_planner_and_pushes_plan(rig):
    manager, store, substrate, notify, tmp_path = rig
    write_convo_lines(tmp_path, 3)

    task = asyncio.create_task(manager.plan_stint())
    await wait_for_spawn(substrate)
    spec = substrate.spawned[0].spec
    assert (spec.name, spec.model, spec.effort) == ("plan", "opus", "high")
    assert spec.plugin_dir == Path("/plugins/claude-code")
    output = output_path(spec)
    assert spec.initial_prompt == (
        f"/voice-code:role-stint-plan convo={tmp_path / 'convo.jsonl'}"
        f" since_line=0 output={output}"
    )
    assert output.parent == tmp_path / "data" / "plans"
    assert store.get_marker() == 3  # current line count recorded at plan time

    output.write_text(PLAN_TEXT)
    await task
    assert substrate.killed == ["voice:plan"]
    assert output.exists()  # the plan file is kept for launch
    (plan,) = notify.messages
    assert plan.type == "stint_plan"
    assert plan.title == "Wire the auth flow"
    assert plan.text == PLAN_TEXT
    assert output.name == f"plan-{plan.plan_id}.md"


async def test_plan_stint_since_line_comes_from_stored_marker(rig):
    manager, store, substrate, notify, tmp_path = rig
    store.set_marker(7)
    write_convo_lines(tmp_path, 9)
    task = asyncio.create_task(manager.plan_stint())
    await wait_for_spawn(substrate)
    assert "since_line=7" in substrate.spawned[0].spec.initial_prompt
    assert store.get_marker() == 9
    output_path(substrate.spawned[0].spec).write_text(PLAN_TEXT)
    await task


async def test_plan_stint_timeout_kills_planner_and_pushes_error(rig):
    manager, store, substrate, notify, tmp_path = rig
    manager._poll_budget = 0.03
    await manager.plan_stint()  # nobody writes the output file
    assert substrate.killed == ["voice:plan"]
    (error,) = notify.messages
    assert error.type == "error"
    assert "timed out" in error.message


async def test_launch_workstream_pastes_plan_and_persists(rig):
    manager, store, substrate, notify, tmp_path = rig
    plan = await launch(rig)
    (session,) = substrate.spawned
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
    assert row.plan_path == str(plan)
    assert row.status == "running"

    (cards,) = notify.messages
    assert cards.type == "workstreams"
    (card,) = cards.workstreams
    assert (card.name, card.title, card.status) == (
        "ws-wire-the-auth-flow", "Wire the auth flow", "running",
    )


async def test_launch_unknown_plan_pushes_error(rig):
    manager, store, substrate, notify, tmp_path = rig
    await manager.launch_workstream("nope")
    (error,) = notify.messages
    assert error.type == "error" and "unknown plan" in error.message
    assert substrate.spawned == []


async def test_send_to_workstream_ferries_directive_and_advances_marker(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    ws_session = substrate.spawned[0]
    store.set_marker(2)
    write_convo_lines(tmp_path, 5)

    task = asyncio.create_task(manager.send_to_workstream("ws-wire-the-auth-flow"))
    await wait_for_spawn(substrate, count=2)
    spec = substrate.spawned[1].spec
    assert (spec.name, spec.model, spec.effort) == ("inject", "opus", "high")
    output = output_path(spec)
    assert spec.initial_prompt == (
        f"/voice-code:role-inject convo={tmp_path / 'convo.jsonl'} since_line=2"
        f" workstream={ws_session.transcript} output={output}"
    )
    output.write_text("Focus the retry loop on idempotent writes only.")
    await task
    assert substrate.killed == ["voice:inject"]
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
    assert substrate.killed == ["voice:inject"]
    assert store.get_marker() == 2  # nothing was delivered; the delta is not lost


async def test_send_to_unknown_workstream_pushes_error(rig):
    manager, store, substrate, notify, tmp_path = rig
    await manager.send_to_workstream("ws-nope")
    (error,) = notify.messages
    assert error.type == "error" and "unknown workstream" in error.message


def transcript_lines() -> str:
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Fix the login bug"},
         "timestamp": "t1"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "On it — reading the auth module."}]},
         "timestamp": "t2"},
        {"type": "assistant",
         "message": {"content": [{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "git status", "description": "Check status"}}]},
         "timestamp": "t3"},
        {"type": "system", "subtype": "turn_duration", "timestamp": "t4"},
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


async def test_cards_carry_tail_last_activity_and_status(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[0]
    session.transcript.parent.mkdir(parents=True, exist_ok=True)
    session.transcript.write_text(transcript_lines())

    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert card.tail == [
        "» Fix the login bug",
        "On it — reading the auth module.",
        "Bash: Check status",
    ]
    assert card.last_activity == "t4"  # TurnEnd still counts as activity
    assert card.status == "running"

    substrate.alive_windows.discard(session.window)  # the window died
    await manager.push_cards()
    (card,) = notify.messages[-1].workstreams
    assert card.status == "gone"
    assert store.list_workstreams()[0].status == "gone"


async def test_manager_rehydrates_from_store(rig):
    manager, store, substrate, notify, tmp_path = rig
    await launch(rig)
    session = substrate.spawned[0]

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
