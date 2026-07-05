import pydantic
import pytest

from voicecode import protocol

CLIENT_MESSAGES = [
    protocol.Hello(credential="cred-1"),
    protocol.TextInput(text="hi"),
    protocol.Mute(muted=True),
    protocol.PlanStint(),
    protocol.LaunchWorkstream(plan_id="p1"),
    protocol.SendToWorkstream(workstream="ws-auth"),
    protocol.CheckIn(workstream="ws-auth"),
    protocol.EndWorkstream(workstream="ws-auth"),
    protocol.Compact(),
    protocol.Approval(approval_id="a1", approved=True),
]

SERVER_MESSAGES = [
    protocol.Ready(),
    protocol.State(state="thinking"),
    protocol.Chat(role="assistant", text="hi there", ts="2026-07-03T12:00:00Z", final=True),
    protocol.Chat(role="activity", text="Bash: git status", ts="2026-07-03T12:00:01Z", final=True),
    protocol.SpeechEnd(),
    protocol.Workstreams(
        workstreams=[
            protocol.WorkstreamCard(
                name="ws-auth",
                title="Wire the auth flow",
                status="running",
                last_activity="2026-07-03T12:00:00Z",
                tail=["» do the thing", "Bash: git status"],
            )
        ]
    ),
    protocol.StintPlan(plan_id="p1", title="Wire the auth flow", text="Stint: Wire the auth flow"),
    protocol.ApprovalRequest(approval_id="a1", session="s1", tool="Bash", summary="rm -rf /tmp/x"),
    protocol.Error(message="nope"),
]


@pytest.mark.parametrize("msg", CLIENT_MESSAGES, ids=lambda m: m.type)
def test_client_message_roundtrip(msg):
    assert protocol.parse_client_message(msg.model_dump_json()) == msg


@pytest.mark.parametrize("msg", SERVER_MESSAGES, ids=lambda m: f"{m.type}-{id(m)}")
def test_server_message_roundtrip(msg):
    assert protocol.parse_server_message(msg.model_dump_json()) == msg


def test_ready_declares_audio_formats():
    ready = protocol.Ready()
    assert ready.mic_format.sample_rate == 16000
    assert ready.tts_format.sample_rate == 24000
    assert ready.mic_format.encoding == "pcm_s16le"


def test_client_message_rejects_server_types():
    with pytest.raises(pydantic.ValidationError):
        protocol.parse_client_message('{"type": "ready"}')


def test_server_message_rejects_unknown_type():
    with pytest.raises(pydantic.ValidationError):
        protocol.parse_server_message('{"type": "bogus"}')
