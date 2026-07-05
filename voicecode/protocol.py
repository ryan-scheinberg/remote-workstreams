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

from pydantic import BaseModel, Field, TypeAdapter


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


class EndWorkstream(BaseModel):
    """Kill the workstream's tmux window and drop its card. The CC transcript survives."""

    type: Literal["end_workstream"] = "end_workstream"
    workstream: str


class Compact(BaseModel):
    type: Literal["compact"] = "compact"


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
        NewWorkstream,
        SendToWorkstream,
        CheckIn,
        EndWorkstream,
        Compact,
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
    role: Literal["user", "assistant", "activity"]
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


class Workstreams(BaseModel):
    type: Literal["workstreams"] = "workstreams"
    workstreams: list[WorkstreamCard]


class ConvoCleared(BaseModel):
    """A fresh convo session is live; the client wipes its chat."""

    type: Literal["convo_cleared"] = "convo_cleared"


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
    Union[Ready, State, Chat, SpeechEnd, Workstreams, ConvoCleared, ApprovalRequest, Error],
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(data: str | bytes) -> ClientMessage:
    return _client_adapter.validate_json(data)


def parse_server_message(data: str | bytes) -> ServerMessage:
    return _server_adapter.validate_json(data)
