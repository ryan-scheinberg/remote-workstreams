import json

import pytest

from remote_workstreams.transcript import (
    AssistantText,
    CompactEnd,
    SessionVitals,
    ToolActivity,
    TranscriptTail,
    TurnEnd,
    UserText,
    parse_line,
)

TS = "2026-07-03T10:00:00.000Z"


def line(**fields) -> str:
    return json.dumps(fields)


def test_user_string_content():
    raw = line(type="user", timestamp=TS, message={"role": "user", "content": "ship the auth slice"})
    assert parse_line(raw) == [UserText(text="ship the auth slice", ts=TS)]


def test_user_list_content_is_tool_result_not_chat():
    raw = line(
        type="user",
        timestamp=TS,
        message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    )
    assert parse_line(raw) == []


def test_user_command_caveat_skipped():
    raw = line(type="user", message={"content": "<command-name>/compact</command-name>"})
    assert parse_line(raw) == []


def test_assistant_thinking_text_and_tool_use():
    raw = line(
        type="assistant",
        timestamp=TS,
        message={
            "model": "claude-fable-5",
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "On it."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "git status", "description": "Show working tree status"},
                },
            ],
        },
    )
    assert parse_line(raw) == [
        AssistantText(text="On it.", ts=TS),
        ToolActivity(label="Bash: Show working tree status", ts=TS),
    ]


def test_bash_label_without_description_truncates_command():
    raw = line(
        type="assistant",
        message={"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x" * 100}}]},
    )
    [entry] = parse_line(raw)
    assert entry.label == "Bash: " + "x" * 60


def test_file_tool_label_uses_basename():
    raw = line(
        type="assistant",
        message={
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/Users/alice/x/remote_workstreams/audio/pipeline.py"},
                }
            ]
        },
    )
    assert parse_line(raw) == [ToolActivity(label="Read: pipeline.py", ts="")]


def test_other_tool_label_is_just_the_name():
    raw = line(
        type="assistant",
        message={"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "x"}}]},
    )
    assert parse_line(raw) == [ToolActivity(label="WebSearch", ts="")]


def test_sidechain_skipped():
    raw = line(
        type="assistant",
        isSidechain=True,
        message={"content": [{"type": "text", "text": "subagent chatter"}]},
    )
    assert parse_line(raw) == []


@pytest.mark.parametrize(
    "meta",
    [
        "ai-title",
        "agent-name",
        "last-prompt",
        "custom-title",
        "mode",
        "permission-mode",
        "attachment",
        "file-history-snapshot",
        "summary",
    ],
)
def test_meta_types_skipped(meta):
    assert parse_line(line(type=meta, timestamp=TS)) == []


def test_system_turn_duration_is_turn_end():
    raw = line(type="system", subtype="turn_duration", timestamp=TS, durationMs=1234)
    assert parse_line(raw) == [TurnEnd(ts=TS)]


def test_system_compact_boundary_is_compact_end():
    raw = line(type="system", subtype="compact_boundary", timestamp=TS, content="Conversation compacted")
    assert parse_line(raw) == [CompactEnd(ts=TS)]


def test_compact_summary_is_not_chat():
    # /compact writes the recap as a user line; it must never render as the user speaking.
    raw = line(
        type="user",
        isCompactSummary=True,
        message={"content": "This session is being continued from a previous conversation..."},
    )
    assert parse_line(raw) == []


def test_typed_slash_command_echo_is_not_chat():
    # The raw "/compact" user line the TUI records when a slash command is typed.
    raw = line(type="user", message={"content": "/compact"})
    assert parse_line(raw) == []


def test_system_other_subtypes_skipped():
    raw = line(type="system", subtype="stop_hook_summary", timestamp=TS)
    assert parse_line(raw) == []


def test_garbage_lines():
    assert parse_line("{not json") == []
    assert parse_line("") == []
    assert parse_line("[1, 2]") == []


def test_missing_timestamp_becomes_empty_string():
    raw = line(type="user", message={"content": "hello there"})
    assert parse_line(raw) == [UserText(text="hello there", ts="")]


def test_tail_missing_file(tmp_path):
    tail = TranscriptTail(tmp_path / "nope.jsonl")
    assert tail.read_new() == []


def test_tail_incremental_reads(tmp_path):
    path = tmp_path / "t.jsonl"
    tail = TranscriptTail(path)
    path.write_text(line(type="user", message={"content": "first"}) + "\n")
    assert tail.read_new() == [UserText(text="first", ts="")]
    assert tail.read_new() == []
    with path.open("a") as f:
        f.write(line(type="assistant", message={"content": [{"type": "text", "text": "reply"}]}) + "\n")
    assert tail.read_new() == [AssistantText(text="reply", ts="")]


def test_tail_holds_partial_last_line(tmp_path):
    path = tmp_path / "t.jsonl"
    tail = TranscriptTail(path)
    full = line(type="user", message={"content": "complete"})
    partial = line(type="user", message={"content": "still writing"})
    path.write_text(full + "\n" + partial[:10])
    assert tail.read_new() == [UserText(text="complete", ts="")]
    with path.open("a") as f:
        f.write(partial[10:] + "\n")
    assert tail.read_new() == [UserText(text="still writing", ts="")]


# ---- SessionVitals ----


def prompt(text="do the thing"):
    return line(type="user", message={"content": text})


def reply(text="on it", usage=None, **fields):
    message = {"content": [{"type": "text", "text": text}]}
    if usage is not None:
        message["usage"] = usage
    return line(type="assistant", message=message, **fields)


def turn_end():
    return line(type="system", subtype="turn_duration", durationMs=1)


def agent_call(tool_id, name="Agent"):
    return line(
        type="assistant",
        message={"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}]},
    )


def agent_result(tool_id, text="done: shipped"):
    return line(
        type="user",
        message={"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
    )


def vitals_from(tmp_path, *lines):
    path = tmp_path / "v.jsonl"
    path.write_text("".join(raw + "\n" for raw in lines))
    vitals = SessionVitals(path)
    vitals.refresh()
    return vitals


def test_vitals_missing_file_is_waiting(tmp_path):
    vitals = SessionVitals(tmp_path / "nope.jsonl")
    vitals.refresh()
    assert (vitals.state, vitals.active_agents, vitals.context_pct) == ("waiting", 0, None)


def test_vitals_turn_in_flight_then_waiting(tmp_path):
    vitals = vitals_from(tmp_path, prompt(), reply())
    assert vitals.state == "thinking"
    vitals = vitals_from(tmp_path, prompt(), reply(), turn_end())
    assert vitals.state == "waiting"


def test_vitals_context_pct_from_last_usage(tmp_path):
    usage = {
        "input_tokens": 2,
        "cache_read_input_tokens": 60_000,
        "cache_creation_input_tokens": 6_000,
        "output_tokens": 998,
    }
    vitals = vitals_from(tmp_path, prompt(), reply(usage=usage), turn_end())
    assert vitals.context_pct == 7  # 67_000 / 1_000_000, rounded
    assert vitals.state == "waiting"


def test_vitals_compacting_shows_thinking_until_boundary(tmp_path):
    """The live stuck-card sequence: a typed /compact lands as a bare user line
    and compaction ends with a boundary, never a turn_duration."""
    compacting = [prompt(), reply(), turn_end(), prompt("/compact")]
    vitals = vitals_from(tmp_path, *compacting)
    assert vitals.state == "thinking"  # compaction in progress
    vitals = vitals_from(
        tmp_path,
        *compacting,
        line(type="system", subtype="compact_boundary", compactMetadata={"postTokens": 90_000}),
        line(type="user", isCompactSummary=True, message={"content": "This session is..."}),
    )
    assert vitals.state == "waiting"  # boundary ends it; the card goes green
    assert vitals.context_pct == 9  # 90_000 / 1_000_000


def test_vitals_compact_boundary_resets_context(tmp_path):
    vitals = vitals_from(
        tmp_path,
        reply(usage={"input_tokens": 800_000}),
        turn_end(),
        line(type="system", subtype="compact_boundary", compactMetadata={"postTokens": 120_000}),
        line(type="user", isCompactSummary=True, message={"content": "This session is..."}),
    )
    assert vitals.context_pct == 12
    assert vitals.state == "waiting"  # the compact summary is not a new prompt


def test_vitals_foreground_agent_counts_until_result(tmp_path):
    vitals = vitals_from(tmp_path, prompt(), agent_call("t1"), agent_call("t2"))
    assert vitals.active_agents == 2
    vitals = vitals_from(tmp_path, prompt(), agent_call("t1"), agent_call("t2"), agent_result("t1"))
    assert vitals.active_agents == 1


def test_vitals_background_agent_survives_ack_until_notification(tmp_path):
    ack = agent_result("t1", "Async agent launched successfully.\nagentId: abc123")
    vitals = vitals_from(tmp_path, prompt(), agent_call("t1"), ack, turn_end())
    assert vitals.active_agents == 1
    assert vitals.state == "waiting"  # runs on while the main agent idles
    notification = line(
        type="user",
        message={
            "content": "<task-notification>\n<task-id>abc123</task-id>"
            "\n<tool-use-id>t1</tool-use-id>\n<status>completed</status>"
        },
    )
    vitals = vitals_from(tmp_path, prompt(), agent_call("t1"), ack, turn_end(), notification)
    assert vitals.active_agents == 0
    assert vitals.state == "waiting"  # the notification line is not a prompt


def test_vitals_api_error_holds_until_recovery(tmp_path):
    vitals = vitals_from(tmp_path, prompt(), reply(isApiErrorMessage=True))
    assert vitals.state == "error"
    vitals = vitals_from(tmp_path, prompt(), reply(isApiErrorMessage=True), reply())
    assert vitals.state == "thinking"  # a successful line clears it
    vitals = vitals_from(tmp_path, prompt(), reply(isApiErrorMessage=True), prompt())
    assert vitals.state == "thinking"  # so does a fresh prompt


def test_vitals_sidechain_and_garbage_ignored(tmp_path):
    vitals = vitals_from(
        tmp_path,
        prompt(),
        line(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "x"}]}),
        "{not json",
        turn_end(),
    )
    assert vitals.state == "waiting"


def test_vitals_incremental_holds_partial_last_line(tmp_path):
    path = tmp_path / "v.jsonl"
    vitals = SessionVitals(path)
    partial = turn_end()
    path.write_text(prompt() + "\n" + partial[:5])
    vitals.refresh()
    assert vitals.state == "thinking"
    with path.open("a") as f:
        f.write(partial[5:] + "\n")
    vitals.refresh()
    assert vitals.state == "waiting"
