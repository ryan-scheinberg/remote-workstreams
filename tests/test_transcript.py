import json

import pytest

from remote_workstreams.transcript import (
    AssistantText,
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
