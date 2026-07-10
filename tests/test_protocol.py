import pydantic
import pytest

from remote_workstreams import protocol

CLIENT_MESSAGES = [
    protocol.Hello(credential="cred-1"),
    protocol.TextInput(text="hi"),
    protocol.Mute(muted=True),
    protocol.NewWorkstream(),
    protocol.SendToWorkstream(workstream="ws-auth"),
    protocol.CheckIn(workstream="ws-auth"),
    protocol.WorkstreamInput(workstream="ws-auth", text="ship the retry loop first"),
    protocol.WatchWorkstream(workstream="ws-auth"),
    protocol.WatchWorkstream(workstream=None),
    protocol.EndWorkstream(workstream="ws-auth"),
    protocol.Compact(),
    protocol.CompactWorkstream(workstream="ws-auth"),
    protocol.SetModel(target="convo", model="sonnet"),
    protocol.SetModel(target="convo", model="gpt-5.6-sol"),
    protocol.SetModel(target="workstream", model="opus"),
    protocol.SetModel(target="plans", model="gpt-5.6-luna"),
    protocol.ClearConvo(),
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
                state="thinking",
                agents=2,
                context_pct=41,
                model="opus",
            ),
            protocol.WorkstreamCard(
                name="ws-docs",
                title="Write the docs",
                status="running",
                model="gpt-5.6-luna",
                engine="codex",
            ),
        ],
        convo_context_pct=17,
        convo_model="sonnet",
        workstream_model="fable",
    ),
    protocol.WorkstreamLog(
        workstream="ws-auth",
        entries=[protocol.LogEntry(role="assistant", text="On it.", ts="2026-07-03T12:00:02Z")],
        reset=True,
    ),
    protocol.ConvoCleared(),
    protocol.Compacted(),
    protocol.ApprovalRequest(approval_id="a1", session="s1", tool="Bash", summary="rm -rf /tmp/x"),
    protocol.Error(message="nope"),
]


@pytest.mark.parametrize("msg", CLIENT_MESSAGES, ids=lambda m: f"{m.type}-{id(m)}")
def test_client_message_roundtrip(msg):
    assert protocol.parse_client_message(msg.model_dump_json()) == msg


def test_set_model_rejects_unknown_models_and_targets():
    with pytest.raises(pydantic.ValidationError):
        protocol.parse_client_message('{"type": "set_model", "target": "convo", "model": "gpt"}')
    with pytest.raises(pydantic.ValidationError):
        protocol.parse_client_message('{"type": "set_model", "target": "planner", "model": "opus"}')


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
