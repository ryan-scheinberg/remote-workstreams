"""Codex rollout parsing, pinned against real lines from codex-cli 0.142.5
sessions (trimmed; shapes verbatim)."""

import json

from remote_workstreams.rollout import RolloutVitals, parse_line
from remote_workstreams.transcript import AssistantText, ToolActivity, TurnEnd, UserText


def line(type_: str, **payload) -> str:
    return json.dumps({"timestamp": "2026-07-07T19:55:47.929Z", "type": type_, "payload": payload})


def test_user_message_is_user_text():
    raw = line("event_msg", type="user_message", message="what role are you?", images=[])
    assert parse_line(raw) == [UserText(text="what role are you?", ts="2026-07-07T19:55:47.929Z")]


def test_agent_message_is_assistant_text_in_both_phases():
    for phase in ("final_answer", "commentary"):
        raw = line("event_msg", type="agent_message", message="On it.", phase=phase)
        (entry,) = parse_line(raw)
        assert isinstance(entry, AssistantText) and entry.text == "On it."


def test_task_complete_is_turn_end():
    raw = line("event_msg", type="task_complete", turn_id="t1", duration_ms=2953)
    assert parse_line(raw) == [TurnEnd(ts="2026-07-07T19:55:47.929Z")]


def test_function_call_is_tool_activity_with_the_command():
    raw = line(
        "response_item",
        type="function_call",
        name="exec_command",
        arguments=json.dumps({"cmd": "pwd && rg --files", "workdir": "/Users/alice"}),
    )
    (entry,) = parse_line(raw)
    assert entry == ToolActivity(label="exec_command: pwd && rg --files", ts=entry.ts)


def test_custom_tool_call_is_tool_activity_by_name():
    raw = line("response_item", type="custom_tool_call", name="apply_patch", input="*** Begin Patch")
    (entry,) = parse_line(raw)
    assert entry.label == "apply_patch"


def test_response_item_message_is_skipped_as_agent_message_duplicate():
    raw = line(
        "response_item",
        type="message",
        role="assistant",
        content=[{"type": "output_text", "text": "dup"}],
    )
    assert parse_line(raw) == []


def test_noise_yields_nothing():
    for raw in ["not json", "[]", json.dumps({"type": "session_meta", "payload": {"id": "x"}}),
                line("event_msg", type="token_count", info={}), line("turn_context", turn_id="t")]:
        assert parse_line(raw) == []


def write_lines(path, *raws):
    path.write_text("".join(raw + "\n" for raw in raws))


def test_vitals_turn_cycle_and_context(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    vitals = RolloutVitals(rollout)
    vitals.refresh()  # missing file: stays at defaults
    assert (vitals.state, vitals.active_agents, vitals.context_pct) == ("waiting", 0, None)

    write_lines(
        rollout,
        line("event_msg", type="task_started", turn_id="t1", model_context_window=258400),
    )
    vitals.refresh()
    assert vitals.state == "thinking"

    write_lines(
        rollout,
        line("event_msg", type="task_started", turn_id="t1", model_context_window=258400),
        line(
            "event_msg",
            type="token_count",
            info={"last_token_usage": {"total_tokens": 129200}, "model_context_window": 258400},
        ),
        line("event_msg", type="task_complete", turn_id="t1"),
    )
    vitals = RolloutVitals(rollout)
    vitals.refresh()
    assert (vitals.state, vitals.context_pct) == ("waiting", 50)


def test_vitals_error_flags_until_the_next_turn(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    write_lines(rollout, line("event_msg", type="error", message="stream disconnected"))
    vitals = RolloutVitals(rollout)
    vitals.refresh()
    assert vitals.state == "error"

    write_lines(
        rollout,
        line("event_msg", type="error", message="stream disconnected"),
        line("event_msg", type="task_started", turn_id="t2", model_context_window=258400),
    )
    vitals.refresh()
    assert vitals.state == "thinking"


def test_vitals_counts_v1_and_v2_children_from_their_rollouts(tmp_path):
    parent = tmp_path / "rollout-parent.jsonl"
    child_one = tmp_path / "rollout-child-one.jsonl"
    child_two = tmp_path / "rollout-child-two.jsonl"
    unrelated = tmp_path / "rollout-unrelated.jsonl"
    parent.write_text(json.dumps({"type": "session_meta", "payload": {"id": "parent"}}) + "\n")

    def child(path, child_id, parent_id, version, *events):
        meta = {
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "parent_thread_id": parent_id,
                "multi_agent_version": version,
            },
        }
        write_lines(path, json.dumps(meta), *events)

    started_one = line("event_msg", type="task_started", turn_id="one")
    started_two = line("event_msg", type="task_started", turn_id="two")
    child(child_one, "one", "parent", "v1", started_one)
    child(child_two, "two", "parent", "v2", started_two)
    child(unrelated, "other", "someone-else", "v2", started_one)

    vitals = RolloutVitals(parent)
    vitals.refresh()
    assert vitals.active_agents == 2

    with child_one.open("a") as f:
        f.write(line("event_msg", type="task_complete", turn_id="one") + "\n")
    vitals.refresh()
    assert vitals.active_agents == 1

    with child_two.open("a") as f:
        f.write(line("event_msg", type="task_complete", turn_id="two") + "\n")
    vitals.refresh()
    assert vitals.active_agents == 0


def test_vitals_waits_out_a_partial_last_line(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    complete = line("event_msg", type="task_started", turn_id="t1", model_context_window=100)
    rollout.write_text(complete + "\n" + '{"type": "event_msg", "payload": {"type": "task_')
    vitals = RolloutVitals(rollout)
    vitals.refresh()
    assert vitals.state == "thinking"  # the torn tail is not consumed

    with rollout.open("a") as f:
        f.write('complete", "turn_id": "t1"}}\n')
    vitals.refresh()
    assert vitals.state == "waiting"
