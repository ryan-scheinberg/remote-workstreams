"""Distill raw Claude Agent SDK traffic into speakable status events.

Dumb templating only — no LLM calls, no network. `summary` is one short speakable
sentence; `detail` carries the exact command/text for the Workspace Viewer.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from voicecode.events import Completed, ErrorEvent, Finding, Progress

_MAX_SUMMARY = 140
_MAX_DETAIL = 2000
_MIN_FINDING_CHARS = 60
_DEBOUNCE_WINDOW_S = 3.0

_TEST_COMMAND = re.compile(r"\b(pytest|vitest|jest|cargo test|go test|npm test|yarn test)\b")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_PLAIN_WORD = re.compile(r"[a-z][a-z0-9_-]*")
_SIMPLE_PATTERN = re.compile(r"[\w .-]{1,40}")
_FIRST_SENTENCE = re.compile(r"(.+?[.!?])(?:\s|$)")

# Launcher/wrapper tokens skipped when naming what a shell command runs.
_COMMAND_WRAPPERS = {
    "sudo", "env", "time", "uv", "uvx", "npx", "poetry", "run", "exec",
    "python", "python3", "node",
}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _speakable(text: str) -> str:
    """First sentence, markdown stripped, capped — safe to hand to TTS."""
    t = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    t = re.sub(r"[`*_#>|\[\]]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    match = _FIRST_SENTENCE.match(t)
    sentence = match.group(1) if match else t
    if len(sentence) > _MAX_SUMMARY:
        sentence = sentence[: _MAX_SUMMARY - 1].rstrip() + "…"
    if sentence and sentence[-1] not in ".!?…":
        sentence += "."
    return sentence or "Update from the execution agent."


def _command_phrase(command: str) -> str:
    """Name what a shell command runs, e.g. 'the test suite', 'git commit', 'ruff check'."""
    if not command:
        return "a shell command"
    if _TEST_COMMAND.search(command):
        return "the test suite"
    tokens = command.split()
    while tokens and (
        _ENV_ASSIGNMENT.fullmatch(tokens[0]) or _basename(tokens[0]) in _COMMAND_WRAPPERS
    ):
        tokens.pop(0)
    if not tokens:
        return "a shell command"
    phrase = _basename(tokens[0])
    if len(tokens) > 1 and _PLAIN_WORD.fullmatch(tokens[1]):
        phrase += f" {tokens[1]}"
    return phrase


def _humanize_tool(name: str) -> str:
    last = name.split("__")[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last)
    words = re.sub(r"[_-]+", " ", words).strip()
    return words.lower() or name


def describe_tool(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """(speakable summary, viewer detail) for one tool call."""
    raw = _clip(json.dumps(tool_input, default=str), _MAX_DETAIL)
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        return f"Running {_command_phrase(command)}.", _clip(command, _MAX_DETAIL) or raw
    if tool_name in {"Read", "NotebookRead"}:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return f"Reading {_basename(path) or 'a file'}.", path or raw
    if tool_name == "Write":
        path = str(tool_input.get("file_path", ""))
        return f"Writing {_basename(path) or 'a file'}.", path or raw
    if tool_name in {"Edit", "MultiEdit", "NotebookEdit"}:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return f"Editing {_basename(path) or 'a file'}.", path or raw
    if tool_name in {"Grep", "Glob"}:
        pattern = str(tool_input.get("pattern", ""))
        if pattern and _SIMPLE_PATTERN.fullmatch(pattern):
            return f"Searching the code for {pattern}.", raw
        return "Searching the code.", raw
    if tool_name in {"Task", "Agent"}:
        desc = str(tool_input.get("description") or "a subtask")
        prompt = _clip(str(tool_input.get("prompt", "")), _MAX_DETAIL)
        return f"Delegating {desc} to a subagent.", prompt or raw
    if tool_name == "WebSearch":
        query = str(tool_input.get("query", "")).strip()
        return (f"Searching the web for {_clip(query, 60)}." if query else "Searching the web."), raw
    if tool_name == "WebFetch":
        host = urlparse(str(tool_input.get("url", ""))).netloc
        return (f"Fetching a page from {host}." if host else "Fetching a web page."), raw
    if tool_name == "TodoWrite":
        return "Updating the task list.", raw
    if tool_name == "Skill":
        skill = str(tool_input.get("skill", "")).strip()
        return (f"Loading the {skill} skill." if skill else "Loading a skill."), raw
    return f"Using the {_humanize_tool(tool_name)} tool.", raw


def describe_gate(
    tool_name: str, tool_input: dict[str, Any], title: str | None = None
) -> tuple[str, str]:
    """(speakable summary, exact-action detail) for a NeedsApproval gate."""
    action, _ = describe_tool(tool_name, tool_input)
    summary = f"Approval needed: {action[0].lower()}{action[1:]}"
    if tool_name == "Bash":
        exact = str(tool_input.get("command", ""))
    else:
        exact = json.dumps(tool_input, default=str)
    detail = _clip(exact, _MAX_DETAIL)
    if title:
        detail = f"{title}\n{detail}"
    return summary, detail


def describe_task(message: str) -> str:
    """Speakable TaskStarted summary for a user prompt entering the session."""
    return _speakable(f"Starting on: {message}")


class Distiller:
    """Stateful per-session distillation. Debounces bursts of the same tool."""

    def __init__(
        self,
        *,
        window: float = _DEBOUNCE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window
        self._clock = clock
        self._last_tool: str | None = None
        self._last_ts = float("-inf")

    def tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> Progress | None:
        now = self._clock()
        if tool_name == self._last_tool and now - self._last_ts < self._window:
            self._last_ts = now
            return None
        self._last_tool = tool_name
        self._last_ts = now
        summary, detail = describe_tool(tool_name, tool_input)
        return Progress(summary=summary, detail=detail)

    def assistant_text(self, text: str) -> Finding | None:
        stripped = text.strip()
        if len(stripped) < _MIN_FINDING_CHARS:
            return None
        return Finding(summary=_speakable(stripped), detail=_clip(stripped, _MAX_DETAIL))

    def turn_result(self, result: str | None, is_error: bool) -> Completed | ErrorEvent:
        self._last_tool = None  # next turn's first tool call always emits
        detail = _clip(result, _MAX_DETAIL) if result else None
        if is_error:
            summary = _speakable(result) if result else "The task failed."
            return ErrorEvent(summary=summary, detail=detail)
        summary = _speakable(result) if result else "Finished the task."
        return Completed(summary=summary, detail=detail)
