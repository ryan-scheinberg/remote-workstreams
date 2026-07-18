"""Codex CLI rollout JSONL parsing — the only module that reads that format.

The Codex analog of remote_workstreams.transcript, pinned to the shape observed
on codex-cli 0.142.5 (~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl,
one JSON object per line). Yields the same Entry types, so ConvoBridge and the
phone cards work unchanged. parse_line never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from remote_workstreams.transcript import (
    AssistantText,
    Entry,
    ToolActivity,
    TurnEnd,
    UserText,
    VitalsState,
    read_complete_lines,
)


def _tool_label(payload: dict) -> str:
    name = payload.get("name") or "tool"
    try:
        args = json.loads(payload.get("arguments") or "{}")
    except ValueError:
        return name
    cmd = args.get("cmd") if isinstance(args, dict) else None
    return f"{name}: {cmd[:60]}" if isinstance(cmd, str) and cmd else name


def parse_line(raw: str) -> list[Entry]:
    """Parse one rollout line into chat entries; [] for everything else.

    event_msg lines carry the user-facing stream: user_message, agent_message
    (final_answer and mid-turn commentary both read as assistant text, matching
    Claude's mid-turn text blocks), and task_complete ends the turn. Tool calls
    appear only as response_item function_call / custom_tool_call lines;
    response_item message lines duplicate agent_message and are skipped.
    """
    try:
        line = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(line, dict):
        return []
    payload = line.get("payload")
    if not isinstance(payload, dict):
        return []
    ts = line.get("timestamp") or ""
    kind = (line.get("type"), payload.get("type"))
    if kind == ("event_msg", "user_message") and isinstance(payload.get("message"), str):
        return [UserText(text=payload["message"], ts=ts)]
    if kind == ("event_msg", "agent_message") and isinstance(payload.get("message"), str):
        return [AssistantText(text=payload["message"], ts=ts)]
    if kind == ("event_msg", "task_complete"):
        return [TurnEnd(ts=ts)]
    if kind == ("response_item", "function_call"):
        return [ToolActivity(label=_tool_label(payload), ts=ts)]
    if kind == ("response_item", "custom_tool_call"):
        return [ToolActivity(label=str(payload.get("name") or "tool"), ts=ts)]
    return []


class RolloutVitals:
    """Codex session health for the phone's cards — the rollout counterpart of
    transcript.SessionVitals, same surface. task_started/task_complete bound the
    turn, token_count carries the fill and the model's context window, and error
    events flag until the next turn starts. Child sessions identify their parent
    in session_meta, so their own task lifecycle supplies active_agents for both
    Codex multi-agent versions.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._thinking = False
        self._error = False
        self._context_tokens: int | None = None
        self._window: int | None = None
        self._child_offsets: dict[Path, int] = {}
        self._child_active: dict[Path, bool] = {}

    @property
    def state(self) -> VitalsState:
        if self._error:
            return "error"
        return "thinking" if self._thinking else "waiting"

    @property
    def active_agents(self) -> int:
        return sum(self._child_active.values())

    @property
    def context_pct(self) -> int | None:
        if self._context_tokens is None or not self._window:
            return None
        return min(100, round(100 * self._context_tokens / self._window))

    def refresh(self) -> None:
        lines, self._offset = read_complete_lines(self.path, self._offset)
        for raw in lines:
            self._scan(raw)
        self._refresh_children()

    def _refresh_children(self) -> None:
        parent_id = self._session_id()
        if parent_id is None:
            return
        current: set[Path] = set()
        for child in self.path.parent.glob("rollout-*.jsonl"):
            if child == self.path:
                continue
            if child not in self._child_offsets:
                if self._parent_id(child) != parent_id:
                    continue
                self._child_offsets[child] = 0
                self._child_active[child] = False
            current.add(child)
            lines, offset = read_complete_lines(child, self._child_offsets[child])
            self._child_offsets[child] = offset
            for raw in lines:
                self._scan_child(raw, child)
        for child in set(self._child_offsets) - current:
            self._child_offsets.pop(child, None)
            self._child_active.pop(child, None)

    def _session_id(self) -> str | None:
        try:
            with self.path.open() as f:
                line = json.loads(f.readline())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        payload = line.get("payload") if isinstance(line, dict) else None
        session_id = payload.get("id") if isinstance(payload, dict) else None
        return session_id if isinstance(session_id, str) else None

    @staticmethod
    def _parent_id(path: Path) -> str | None:
        try:
            with path.open() as f:
                line = json.loads(f.readline())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        payload = line.get("payload") if isinstance(line, dict) else None
        parent_id = payload.get("parent_thread_id") if isinstance(payload, dict) else None
        return parent_id if isinstance(parent_id, str) else None

    def _scan_child(self, raw: str, path: Path) -> None:
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(line, dict) or line.get("type") != "event_msg":
            return
        payload = line.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "task_started":
            self._child_active[path] = True
        elif kind in ("task_complete", "turn_aborted"):
            self._child_active[path] = False

    def _scan(self, raw: str) -> None:
        try:
            line = json.loads(raw)
        except ValueError:
            return
        if not isinstance(line, dict) or line.get("type") != "event_msg":
            return
        payload = line.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "task_started":
            self._thinking, self._error = True, False
            window = payload.get("model_context_window")
            if isinstance(window, int):
                self._window = window
        elif kind in ("task_complete", "turn_aborted"):
            self._thinking = False
        elif kind == "token_count":
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if isinstance(last, dict) and isinstance(last.get("total_tokens"), int):
                self._context_tokens = last["total_tokens"]
            if isinstance(info, dict) and isinstance(info.get("model_context_window"), int):
                self._window = info["model_context_window"]
        elif kind in ("error", "stream_error"):
            self._error = True
