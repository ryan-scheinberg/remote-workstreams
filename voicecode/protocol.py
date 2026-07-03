"""WebSocket protocol between clients (PWA, local frontend) and the server.

Text frames carry exactly one JSON message from the models below. Binary frames carry
raw audio: client→server is mic PCM, server→client is TTS PCM. Formats are declared
in `Ready` so the client never guesses.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from voicecode.events import StatusEvent


class AudioFormat(BaseModel):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: int
    channels: int = 1


MIC_FORMAT = AudioFormat(sample_rate=16000)
TTS_FORMAT = AudioFormat(sample_rate=24000)


# ---- client → server ----


class Hello(BaseModel):
    """First message on every connection. credential=None is only valid for pairing."""

    type: Literal["hello"] = "hello"
    credential: str | None = None
    session_id: str | None = None  # None = resume most recent session, or start fresh


class TextInput(BaseModel):
    """Typed input; bypasses STT but flows through the same turn machinery."""

    type: Literal["text_input"] = "text_input"
    text: str


class Mute(BaseModel):
    """While muted, proactive speech queues; it surfaces on unmute or in the Viewer."""

    type: Literal["mute"] = "mute"
    muted: bool


class Approval(BaseModel):
    type: Literal["approval"] = "approval"
    gate_id: str
    approved: bool


class SwitchSession(BaseModel):
    type: Literal["switch_session"] = "switch_session"
    session_id: str


ClientMessage = Annotated[
    Union[Hello, TextInput, Mute, Approval, SwitchSession],
    Field(discriminator="type"),
]


# ---- server → client ----


class Ready(BaseModel):
    type: Literal["ready"] = "ready"
    session_id: str
    mic_format: AudioFormat = MIC_FORMAT
    tts_format: AudioFormat = TTS_FORMAT


class State(BaseModel):
    type: Literal["state"] = "state"
    state: Literal["listening", "thinking", "speaking", "interrupted"]


class Transcript(BaseModel):
    type: Literal["transcript"] = "transcript"
    role: Literal["user", "assistant"]
    text: str
    final: bool


class Event(BaseModel):
    """A status event for the Workspace Viewer (approvals render as approve/deny)."""

    type: Literal["event"] = "event"
    event: StatusEvent


class SpeechEnd(BaseModel):
    """The TTS binary stream for the current utterance is complete."""

    type: Literal["speech_end"] = "speech_end"


class SessionInfo(BaseModel):
    id: str
    title: str
    created_at: float
    last_active: float


class Sessions(BaseModel):
    type: Literal["sessions"] = "sessions"
    sessions: list[SessionInfo]


class Error(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Annotated[
    Union[Ready, State, Transcript, Event, SpeechEnd, Sessions, Error],
    Field(discriminator="type"),
]

_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


def parse_client_message(data: str | bytes) -> ClientMessage:
    return _client_adapter.validate_json(data)


def parse_server_message(data: str | bytes) -> ServerMessage:
    return _server_adapter.validate_json(data)
