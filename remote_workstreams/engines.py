"""The two session engines — Claude Code and Codex CLI — and which models run
on which. The model name is the single source of truth: an engine is derived
from its model, everywhere.

Codex model IDs are passed to the CLI as-is. The picker has the same values and
shorter human labels.
"""

from __future__ import annotations

from pathlib import Path

from remote_workstreams import rollout, transcript

CLAUDE_MODELS = ("sonnet", "opus", "fable")
CODEX_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
MODELS = CLAUDE_MODELS + CODEX_MODELS


def engine_of(model: str) -> str:
    return "codex" if model in CODEX_MODELS else "claude"


def tail(path: Path, engine: str) -> transcript.TranscriptTail:
    parse = rollout.parse_line if engine == "codex" else transcript.parse_line
    return transcript.TranscriptTail(path, parse=parse)


def vitals(path: Path, engine: str) -> rollout.RolloutVitals | transcript.SessionVitals:
    return rollout.RolloutVitals(path) if engine == "codex" else transcript.SessionVitals(path)
