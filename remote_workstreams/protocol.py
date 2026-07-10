"""WebSocket protocol between the phone client and the server.

Text frames carry exactly one JSON message from the models below. Binary frames carry
raw audio: client→server is mic PCM, server→client is TTS PCM. Formats are declared
in `Ready` so the client never guesses.

Chat sourcing rule: assistant text, tool activity, and FINAL user text all render from
the convo session's Claude Code transcript (the source of truth); the audio pipeline
emits chat(role="user", final=False) only for STT interims.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from remote_workstreams.engines import MODELS


class AudioFormat(BaseModel):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: int
    channels: int = 1


MIC_FORMAT = AudioFormat(sample_rate=16000)
TTS_FORMAT = AudioFormat(sample_rate=24000)


# ---- client → server ----


class Hello(BaseModel):
    """First message on every connection."""

    type: Literal["hello"] = "hello"
    credential: str | None = None


class TextInput(BaseModel):
    """Typed input; bypasses STT but flows through the same turn machinery."""

    type: Literal["text_input"] = "text_input"
    text: str


class Mute(BaseModel):
    type: Literal["mute"] = "mute"
    muted: bool


class Hush(BaseModel):
    """Speaker mute, the counterpart of Mute's mic mute: while on, replies land
    in chat but are never synthesized — silence costs nothing. Flipping it on
    mid-reply stops the audio immediately."""

    type: Literal["hush"] = "hush"
    muted: bool


class NewWorkstream(BaseModel):
    """Plan a stint from the conversation since the last marker and launch it —
    one button, no plan review on the phone."""

    type: Literal["new_workstream"] = "new_workstream"


class SendToWorkstream(BaseModel):
    """Route the latest conversation delta through the injector into a workstream."""

    type: Literal["send_to_workstream"] = "send_to_workstream"
    workstream: str


class CheckIn(BaseModel):
    """Have the convo session read a workstream's transcript tail and speak the status."""

    type: Literal["check_in"] = "check_in"
    workstream: str


class WorkstreamInput(BaseModel):
    """A message typed in the detail view, queued straight into the workstream
    session — the same paste path injector directives ride, so mid-turn input
    queues and nothing forks."""

    type: Literal["workstream_input"] = "workstream_input"
    workstream: str
    text: str


class WatchWorkstream(BaseModel):
    """Follow one workstream's log in the detail view; None stops the feed."""

    type: Literal["watch_workstream"] = "watch_workstream"
    workstream: str | None = None


class EndWorkstream(BaseModel):
    """Kill the workstream's tmux window and drop its card. The CC transcript survives."""

    type: Literal["end_workstream"] = "end_workstream"
    workstream: str


class Compact(BaseModel):
    type: Literal["compact"] = "compact"


class CompactWorkstream(BaseModel):
    """Type /compact into a workstream session to shrink its context."""

    type: Literal["compact_workstream"] = "compact_workstream"
    workstream: str


class SetModel(BaseModel):
    """Pick a model from the settings menu; the engine rides on the model name.
    convo switches live (a same-engine Claude pick types /model, anything else
    clears and starts fresh); workstream only affects future spawns; plans is
    one pick for both ephemeral passthroughs (planner and injector)."""

    type: Literal["set_model"] = "set_model"
    target: Literal["convo", "workstream", "plans"]
    model: str

    @field_validator("model")
    @classmethod
    def _known(cls, model: str) -> str:
        if model not in MODELS:
            raise ValueError(f"unknown model: {model}")
        return model


class ClearConvo(BaseModel):
    """Replace the convo session with a brand-new one: fresh context, clean role-convo."""

    type: Literal["clear_convo"] = "clear_convo"


class Approval(BaseModel):
    type: Literal["approval"] = "approval"
    approval_id: str
    approved: bool


ClientMessage = Annotated[
    Union[
        Hello,
        TextInput,
        Mute,
        Hush,
        NewWorkstream,
        SendToWorkstream,
        CheckIn,
        WorkstreamInput,
        WatchWorkstream,
        EndWorkstream,
        Compact,
        CompactWorkstream,
        SetModel,
        ClearConvo,
        Approval,
    ],
    Field(discriminator="type"),
]


# ---- server → client ----


class Ready(BaseModel):
    type: Literal["ready"] = "ready"
    mic_format: AudioFormat = MIC_FORMAT
    tts_format: AudioFormat = TTS_FORMAT


class State(BaseModel):
    type: Literal["state"] = "state"
    state: Literal["listening", "thinking", "speaking", "interrupted"]


class Chat(BaseModel):
    type: Literal["chat"] = "chat"
    # "queued" = typed mid-turn, visible before the session takes it; the same
    # text re-renders as "user" on consumption — that duplicate is by design.
    role: Literal["user", "queued", "assistant", "activity"]
    text: str
    ts: str
    final: bool


class SpeechEnd(BaseModel):
    """The TTS binary stream for the current utterance is complete."""

    type: Literal["speech_end"] = "speech_end"


class WorkstreamCard(BaseModel):
    name: str
    title: str
    status: Literal["running", "gone"]
    state: Literal["waiting", "thinking", "error"] = "waiting"  # from the transcript
    agents: int = 0  # subagents currently running
    context_pct: int | None = None  # context fill; None until first usage lands
    model: str = "fable"  # what it was launched with; immutable for its lifetime
    engine: Literal["claude", "codex"] = "claude"


class Workstreams(BaseModel):
    type: Literal["workstreams"] = "workstreams"
    workstreams: list[WorkstreamCard]
    convo_context_pct: int | None = None  # the convo session's fill, for its Compact button
    convo_model: str = "fable"  # current picks, so the settings menu shows them
    workstream_model: str = "fable"
    plans_model: str = "opus"  # the planner+injector pick
    models: list[str] = list(MODELS)  # engines wired on this box; the picker hides the rest


class LogEntry(BaseModel):
    """One workstream transcript line, shaped like Chat without the envelope."""

    role: Literal["user", "queued", "assistant", "activity"]
    text: str
    ts: str


class WorkstreamLog(BaseModel):
    """The watched workstream's log: a replay on watch (reset=True), then
    increments as new transcript lines land."""

    type: Literal["workstream_log"] = "workstream_log"
    workstream: str
    entries: list[LogEntry]
    reset: bool = False


class ConvoCleared(BaseModel):
    """A fresh convo session is live; the client wipes its chat."""

    type: Literal["convo_cleared"] = "convo_cleared"


class Compacted(BaseModel):
    """The convo session finished a /compact; the client stops its spinner."""

    type: Literal["compacted"] = "compacted"


class ApprovalRequest(BaseModel):
    type: Literal["approval_request"] = "approval_request"
    approval_id: str
    session: str
    tool: str
    summary: str


class Error(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Annotated[
    Union[
        Ready,
        State,
        Chat,
        SpeechEnd,
        Workstreams,
        WorkstreamLog,
        ConvoCleared,
        Compacted,
        ApprovalRequest,
        Error,
    ],
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(data: str | bytes) -> ClientMessage:
    return _client_adapter.validate_json(data)


def parse_server_message(data: str | bytes) -> ServerMessage:
    return _server_adapter.validate_json(data)
