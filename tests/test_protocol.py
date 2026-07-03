import pydantic
import pytest

from voicecode.events import Finding
from voicecode.protocol import (
    Approval,
    Event,
    Ready,
    parse_client_message,
    parse_server_message,
)


def test_client_message_roundtrip():
    msg = Approval(gate_id="g1", approved=True)
    assert parse_client_message(msg.model_dump_json()) == msg


def test_ready_declares_audio_formats():
    ready = Ready(session_id="s1")
    assert ready.mic_format.sample_rate == 16000
    assert ready.tts_format.sample_rate == 24000
    assert ready.mic_format.encoding == "pcm_s16le"


def test_status_event_nests_in_server_message():
    msg = Event(event=Finding(summary="The bug is in the retry loop."))
    parsed = parse_server_message(msg.model_dump_json())
    assert parsed.event.type == "finding"


def test_client_message_rejects_server_types():
    with pytest.raises(pydantic.ValidationError):
        parse_client_message('{"type": "ready", "session_id": "s1"}')
