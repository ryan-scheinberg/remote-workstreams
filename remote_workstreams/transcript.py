"""Claude Code transcript JSONL parsing — the only module that reads the format.

The format is undocumented; this pins the shape observed live on Claude Code
2.1.201 (one JSON object per line). parse_line never raises: anything
unrecognized or unparseable yields no entries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserText:
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


Entry = UserText | AssistantText | ToolActivity | TurnEnd


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
    subtype turn_duration, which marks turn completion. User content that is
    a list carries tool_result blocks, not chat; user strings starting with
    "<" are local-command caveats / system-reminder wrappers.
    """
    try:
        line = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(line, dict) or line.get("isSidechain"):
        return []
    ts = line.get("timestamp") or ""
    if line.get("type") == "system":
        return [TurnEnd(ts=ts)] if line.get("subtype") == "turn_duration" else []
    message = line.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if line.get("type") == "user":
        if isinstance(content, str) and content and not content.startswith("<"):
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


class TranscriptTail:
    """Incremental reader over a growing transcript file.

    Byte-offset based; only consumes lines ending in a newline, so a partially
    written last line waits for the next read. Synchronous — poll from async loops.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0

    def read_new(self) -> list[Entry]:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                data = f.read()
        except FileNotFoundError:
            return []
        end = data.rfind(b"\n") + 1
        if end == 0:
            return []
        self._offset += end
        entries: list[Entry] = []
        for raw in data[:end].decode("utf-8", errors="replace").splitlines():
            entries.extend(parse_line(raw))
        return entries
