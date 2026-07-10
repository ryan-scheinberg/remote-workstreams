"""Claude Code transcript JSONL parsing — the only module that reads the format.

The format is undocumented; this pins the shape observed live on Claude Code
2.1.202 (one JSON object per line). parse_line never raises: anything
unrecognized or unparseable yields no entries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class UserText:
    text: str
    ts: str


@dataclass(frozen=True)
class QueuedText:
    """User input typed mid-turn: Claude Code writes a queue-operation line at
    send time, then a normal user line when the session consumes it — both
    render, styled apart, so a sent message is visible before it's taken."""

    text: str
    ts: str


@dataclass(frozen=True)
class AssistantText:
    text: str
    ts: str


@dataclass(frozen=True)
class ToolActivity:
    label: str  # short human string, e.g. "Bash: git status"
    ts: str


@dataclass(frozen=True)
class TurnEnd:
    """Claude Code writes a system/turn_duration line when a turn completes."""

    ts: str


@dataclass(frozen=True)
class CompactEnd:
    """Claude Code writes a system/compact_boundary line when /compact finishes."""

    ts: str


Entry = UserText | QueuedText | AssistantText | ToolActivity | TurnEnd | CompactEnd


def _tool_label(block: dict) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input")
    if not isinstance(inp, dict):
        return name
    if name == "Bash":
        detail = inp.get("description") or (inp.get("command") or "")[:60]
        return f"{name}: {detail}" if detail else name
    if "file_path" in inp:
        return f"{name}: {Path(str(inp['file_path'])).name}"
    return name


def parse_line(raw: str) -> list[Entry]:
    """Parse one transcript line into chat entries; [] for everything else.

    Meta line types (ai-title, file-history-snapshot, ...) and sidechain
    (subagent) lines fall through to []. system lines are skipped except
    subtype turn_duration (turn completion) and compact_boundary (/compact
    completion). User content that is a list carries tool_result blocks, not
    chat; user strings starting with "<" are local-command caveats /
    system-reminder wrappers, "/" is a typed slash command's raw echo, and
    isCompactSummary is compaction's synthetic recap — none of them are speech.
    """
    try:
        line = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(line, dict) or line.get("isSidechain") or line.get("isCompactSummary"):
        return []
    ts = line.get("timestamp") or ""
    if line.get("type") == "queue-operation":
        # enqueue carries the text at top level; dequeue/remove carry nothing.
        # Same speech filter as user lines: task-notifications and slash
        # commands queue too, and they are not chat.
        content = line.get("content")
        if (
            line.get("operation") == "enqueue"
            and isinstance(content, str)
            and content
            and not content.startswith(("<", "/"))
        ):
            return [QueuedText(text=content, ts=ts)]
        return []
    if line.get("type") == "system":
        if line.get("subtype") == "turn_duration":
            return [TurnEnd(ts=ts)]
        if line.get("subtype") == "compact_boundary":
            return [CompactEnd(ts=ts)]
        return []
    message = line.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if line.get("type") == "user":
        if isinstance(content, str) and content and not content.startswith(("<", "/")):
            return [UserText(text=content, ts=ts)]
        return []
    if line.get("type") == "assistant" and isinstance(content, list):
        entries: list[Entry] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                entries.append(AssistantText(text=block["text"], ts=ts))
            elif block.get("type") == "tool_use":
                entries.append(ToolActivity(label=_tool_label(block), ts=ts))
        return entries
    return []


def read_complete_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """New complete lines at byte offset; a partially written last line waits
    for the next read."""
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    if end == 0:
        return [], offset
    return data[:end].decode("utf-8", errors="replace").splitlines(), offset + end


class TranscriptTail:
    """Incremental reader over a growing transcript file. Synchronous — poll
    from async loops. `parse` swaps the line format (rollout.parse_line reads
    Codex sessions into the same entries)."""

    def __init__(self, path: Path, parse: Callable[[str], list[Entry]] = parse_line) -> None:
        self._path = path
        self._parse = parse
        self._offset = 0

    def read_new(self) -> list[Entry]:
        lines, self._offset = read_complete_lines(self._path, self._offset)
        entries: list[Entry] = []
        for raw in lines:
            entries.extend(self._parse(raw))
        return entries


CONTEXT_WINDOW = 1_000_000  # tokens; what the roster's models run with

VitalsState = Literal["waiting", "thinking", "error"]

_AGENT_TOOLS = frozenset({"Agent", "Task"})
# A backgrounded agent's tool_result is only the launch ack; it stays active until
# its <task-notification> completion line, matched by tool_use_id.
_ASYNC_ACK = "Async agent launched"
_NOTIFIED_ID = re.compile(r"<tool-use-id>([^<]*)</tool-use-id>")


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _notification_ids(line: dict) -> list[str]:
    """Tool_use ids a <task-notification> reports finished, wherever it sits.

    A backgrounded agent's completion is a plain `user` line (message.content)
    when the parent is idle, but a queued `queue-operation` line (top-level
    content) when the parent is mid-turn — so match the text, not the line type.
    """
    message = line.get("message")
    carriers = (line.get("content"), message.get("content") if isinstance(message, dict) else None)
    for text in carriers:
        if isinstance(text, str) and "<task-notification>" in text:
            return _NOTIFIED_ID.findall(text)
    return []


class SessionVitals:
    """Session health for the phone's cards, scanned incrementally from the
    transcript: turn in flight, active subagents, context fill. Same format pin
    and partial-last-line rule as TranscriptTail; unrecognized lines change nothing.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._thinking = False
        self._error = False
        self._agents: set[str] = set()
        self._context_tokens: int | None = None

    @property
    def state(self) -> VitalsState:
        if self._error:
            return "error"
        return "thinking" if self._thinking else "waiting"

    @property
    def active_agents(self) -> int:
        return len(self._agents)

    @property
    def context_pct(self) -> int | None:
        if self._context_tokens is None:
            return None
        return min(100, round(100 * self._context_tokens / CONTEXT_WINDOW))

    def refresh(self) -> None:
        lines, self._offset = read_complete_lines(self.path, self._offset)
        for raw in lines:
            self._scan(raw)

    def _scan(self, raw: str) -> None:
        try:
            line = json.loads(raw)
        except ValueError:
            return
        if not isinstance(line, dict) or line.get("isSidechain"):
            return
        for tid in _notification_ids(line):
            self._agents.discard(tid)  # a finished agent notifies from any line type
        if line.get("type") == "system":
            if line.get("subtype") == "turn_duration":
                self._thinking = False
            elif line.get("subtype") == "compact_boundary":
                # Compaction ends here, never with a turn_duration — the typed
                # "/compact" user line set thinking, so clear it or the card
                # shows "compacting" forever.
                self._thinking = False
                post = (line.get("compactMetadata") or {}).get("postTokens")
                if isinstance(post, int):
                    self._context_tokens = post
            return
        message = line.get("message")
        if not isinstance(message, dict):
            return
        if line.get("type") == "assistant":
            self._scan_assistant(line, message)
        elif line.get("type") == "user":
            self._scan_user(line, message)

    def _scan_assistant(self, line: dict, message: dict) -> None:
        if line.get("isApiErrorMessage"):
            self._error = True  # holds until the session produces or receives again
            return
        self._error = False
        self._thinking = True
        usage = message.get("usage")
        if isinstance(usage, dict):
            tokens = sum(
                v
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "output_tokens",
                )
                if isinstance(v := usage.get(key), int)
            )
            if tokens:
                self._context_tokens = tokens
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") in _AGENT_TOOLS
                ):
                    self._agents.add(str(block.get("id")))

    def _scan_user(self, line: dict, message: dict) -> None:
        if line.get("isCompactSummary"):
            return  # compaction's summary line is not a new prompt
        content = message.get("content")
        if isinstance(content, list):
            self._thinking = True  # tool results land only mid-turn
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = str(block.get("tool_use_id"))
                if tid in self._agents and not _result_text(block).startswith(_ASYNC_ACK):
                    self._agents.discard(tid)
        elif isinstance(content, str) and content and not content.startswith("<"):
            self._thinking = True  # a real user line starts a turn
            self._error = False
