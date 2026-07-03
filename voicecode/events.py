"""Typed status events — the bridge from the execution layer to the conversation layer.

The execution adapter distills raw agent activity into these events. The server queues
them and the conversation engine injects pending ones into the next user turn as a
<system-reminder> block (never as role:"system" — that is Opus-only mid-conversation).

`summary` must be speakable: one short plain-English sentence, present tense, no
markdown, no file-path soup. `detail` is for the Workspace Viewer, never spoken verbatim.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


def _event_id() -> str:
    return uuid.uuid4().hex[:12]


class _BaseEvent(BaseModel):
    id: str = Field(default_factory=_event_id)
    ts: float = Field(default_factory=time.time)
    summary: str
    detail: str | None = None


class TaskStarted(_BaseEvent):
    type: Literal["task_started"] = "task_started"


class Progress(_BaseEvent):
    type: Literal["progress"] = "progress"


class Finding(_BaseEvent):
    type: Literal["finding"] = "finding"


class NeedsApproval(_BaseEvent):
    """A gated tool call is waiting on the user. Triggers proactive speech when unmuted."""

    type: Literal["needs_approval"] = "needs_approval"
    gate_id: str
    tool_name: str


class Completed(_BaseEvent):
    """The execution agent finished its task. Triggers proactive speech when unmuted."""

    type: Literal["completed"] = "completed"


class ErrorEvent(_BaseEvent):
    type: Literal["error"] = "error"


StatusEvent = Annotated[
    Union[TaskStarted, Progress, Finding, NeedsApproval, Completed, ErrorEvent],
    Field(discriminator="type"),
]

_adapter: TypeAdapter[StatusEvent] = TypeAdapter(StatusEvent)


def parse_event(data: dict | str | bytes) -> StatusEvent:
    if isinstance(data, dict):
        return _adapter.validate_python(data)
    return _adapter.validate_json(data)
