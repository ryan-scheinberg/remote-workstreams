import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server_fakes import Fakes, make_app
from voicecode.protocol import Approval, Hello, Mute, SwitchSession, TextInput
from voicecode.server.auth import hash_secret


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(tmp_path, fakes):
    app = make_app(tmp_path, fakes)
    with TestClient(app) as client:
        client.app_state = app.state
        client.app_state.store.create_credential("test-device", hash_secret("cred-1"))
        yield client


def hello(ws, credential="cred-1", session_id=None) -> dict:
    ws.send_text(Hello(credential=credential, session_id=session_id).model_dump_json())
    first = json.loads(ws.receive_text())
    if first["type"] != "ready":
        return first  # error path; no sessions push follows
    sessions = json.loads(ws.receive_text())  # pushed right after Ready
    assert sessions["type"] == "sessions"
    assert first["session_id"] in [s["id"] for s in sessions["sessions"]]
    return first


def run_turn(ws, text: str) -> tuple[list[dict], bytes]:
    """One committed turn against the fake pipeline: 5 text frames + 1 binary frame."""
    ws.send_text(TextInput(text=text).model_dump_json())
    frames = [json.loads(ws.receive_text()) for _ in range(3)]  # state, user, assistant
    audio = ws.receive_bytes()
    frames += [json.loads(ws.receive_text()) for _ in range(2)]  # speech_end, state
    return frames, audio


def test_first_frame_must_be_hello(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(TextInput(text="hi").model_dump_json())
        assert json.loads(ws.receive_text()) == {"type": "error", "message": "expected hello"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_binary_first_frame_rejected(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        assert json.loads(ws.receive_text())["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_invalid_credential_gets_error_and_close(client):
    for credential in ["wrong", None]:
        with client.websocket_connect("/ws") as ws:
            msg = hello(ws, credential=credential)
            assert msg == {"type": "error", "message": "invalid credential"}
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_unknown_session_gets_error_and_close(client):
    with client.websocket_connect("/ws") as ws:
        msg = hello(ws, session_id="missing")
        assert msg == {"type": "error", "message": "unknown session"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_ready_then_sink_maps_turn_onto_protocol(client):
    with client.websocket_connect("/ws") as ws:
        ready = hello(ws)
        assert ready["type"] == "ready"
        assert ready["mic_format"]["sample_rate"] == 16000
        assert ready["tts_format"]["sample_rate"] == 24000

        frames, audio = run_turn(ws, "hello world")
        assert [f["type"] for f in frames] == [
            "state", "transcript", "transcript", "speech_end", "state",
        ]
        assert frames[0]["state"] == "thinking"
        assert frames[1] == {
            "type": "transcript", "role": "user", "text": "hello world", "final": True,
        }
        assert frames[2]["role"] == "assistant" and frames[2]["text"] == "echo: hello world"
        assert audio == b"\x01\x02"
        assert frames[4]["state"] == "listening"

        # the committed turn persisted server-side
        store = client.app_state.store
        session = store.get_session(ready["session_id"])
        assert session.title == "hello world"
        assert [m["role"] for m in session.messages] == ["user", "assistant"]
        log = store.get_transcript(ready["session_id"])
        assert [(e["role"], e["text"]) for e in log] == [
            ("user", "hello world"), ("assistant", "echo: hello world"),
        ]


def test_feed_mute_and_approval_are_routed(client, fakes):
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_bytes(b"\x00\x01")
        ws.send_text(Mute(muted=True).model_dump_json())
        ws.send_text(Approval(gate_id="g1", approved=False).model_dump_json())
        run_turn(ws, "sync")  # round-trip so the frames above are processed

        pipeline = fakes.pipelines[-1]
        assert pipeline.fed == [b"\x00\x01"]
        assert pipeline.muted is True
        assert fakes.executions[0].approvals == [("g1", False)]


def test_switch_session(client, fakes):
    with client.websocket_connect("/ws") as ws:
        first = hello(ws)["session_id"]
        other = client.app_state.store.create_session("Other").id

        ws.send_text(SwitchSession(session_id="missing").model_dump_json())
        assert json.loads(ws.receive_text()) == {"type": "error", "message": "unknown session"}

        ws.send_text(SwitchSession(session_id=other).model_dump_json())
        ready = json.loads(ws.receive_text())
        assert ready["type"] == "ready" and ready["session_id"] == other
        sessions = json.loads(ws.receive_text())  # sessions re-pushed after switch
        assert sessions["type"] == "sessions"
        assert other != first
        assert fakes.pipelines[0].closed  # old session's pipeline was torn down

        run_turn(ws, "in the other session")
        assert fakes.pipelines[-1].texts == ["in the other session"]


def test_second_connection_takes_over(client, fakes):
    with client.websocket_connect("/ws") as ws1:
        first = hello(ws1)["session_id"]
        with client.websocket_connect("/ws") as ws2:
            ready2 = hello(ws2)
            assert ready2["session_id"] == first  # same session, new socket
            assert json.loads(ws1.receive_text()) == {
                "type": "error", "message": "another connection took over",
            }
            with pytest.raises(WebSocketDisconnect):
                ws1.receive_text()
            assert fakes.pipelines[0].closed
            run_turn(ws2, "still alive")  # the takeover connection is fully functional


def test_dispatch_starts_execution_and_events_reach_client(client, fakes):
    with client.websocket_connect("/ws") as ws:
        session_id = hello(ws)["session_id"]
        run_turn(ws, "dispatch: run the tests")

        event_frame = json.loads(ws.receive_text())
        assert event_frame["type"] == "event"
        assert event_frame["event"]["type"] == "task_started"

        execution = fakes.executions[0]
        assert execution.started_prompts == ["run the tests"]
        # the same event went into the pipeline for engine injection
        assert [e.type for e in fakes.pipelines[-1].events] == ["task_started"]
        # execution session id persisted for resume
        assert client.app_state.store.get_session(session_id).execution_session_id == "exec-1"

        run_turn(ws, "dispatch: also lint")
        assert execution.sent == ["also lint"]  # second dispatch is a send(), not a new start()
        assert json.loads(ws.receive_text())["event"]["type"] == "progress"


def test_reconnect_reuses_live_runtime(client, fakes):
    with client.websocket_connect("/ws") as ws:
        first = hello(ws)["session_id"]
        run_turn(ws, "hello")
    with client.websocket_connect("/ws") as ws:
        assert hello(ws)["session_id"] == first
    assert len(fakes.engines) == 1  # runtime survived the disconnect in memory
    assert fakes.engines[0].loaded is None


def test_resume_after_server_restart(tmp_path):
    fakes1 = Fakes()
    app1 = make_app(tmp_path, fakes1)
    with TestClient(app1) as client1:
        app1.state.store.create_credential("test-device", hash_secret("cred-1"))
        with client1.websocket_connect("/ws") as ws:
            session_id = hello(ws)["session_id"]
            run_turn(ws, "dispatch: fix the bug")
            assert json.loads(ws.receive_text())["type"] == "event"

    fakes2 = Fakes()
    app2 = make_app(tmp_path, fakes2)  # same sqlite file = restarted server
    with TestClient(app2) as client2:
        with client2.websocket_connect("/ws") as ws:
            ready = hello(ws)  # session_id=None resumes the most recent session
            assert ready["session_id"] == session_id
            assert fakes2.engines[0].loaded == [
                {"role": "user", "content": "dispatch: fix the bug"},
                {"role": "assistant", "content": "echo: dispatch: fix the bug"},
            ]
            assert fakes2.executions[0].resumed == ["exec-1"]
