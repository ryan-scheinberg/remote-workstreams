import json
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server_fakes import FakeConn, Fakes, make_app, seed_session
from remote_workstreams.audio.state import PipelineState
from remote_workstreams.protocol import (
    Approval,
    CheckIn,
    ClearConvo,
    Compact,
    CompactWorkstream,
    Hello,
    Hush,
    Mute,
    NewWorkstream,
    SendToWorkstream,
    SetModel,
    TextInput,
)
from remote_workstreams.server.runtime import ProtocolSink
from remote_workstreams.transcript import AssistantText, CompactEnd, ToolActivity, TurnEnd, UserText


@pytest.fixture
def fakes(tmp_path):
    return Fakes(tmp_path)


@pytest.fixture
def client(tmp_path, fakes):
    app = make_app(tmp_path, fakes)
    with TestClient(app) as client:
        client.app_state = app.state
        seed_session(app.state, "cred-1")
        yield client


def hello(ws, credential="cred-1") -> dict:
    ws.send_text(Hello(credential=credential).model_dump_json())
    return json.loads(ws.receive_text())


def run_turn(ws, text: str) -> tuple[list[dict], bytes]:
    """One turn against the fake pipeline: state, TTS audio, speech_end, state."""
    ws.send_text(TextInput(text=text).model_dump_json())
    frames = [json.loads(ws.receive_text())]  # state: thinking
    audio = ws.receive_bytes()
    frames += [json.loads(ws.receive_text()) for _ in range(2)]  # speech_end, state
    return frames, audio


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


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


def test_hello_rejects_store_era_credentials(client):
    # Passkeys in the store are for login ceremonies, never valid as a WS credential.
    client.app_state.store.create_credential("old-phone", "wcid", b"pk", 0)
    with client.websocket_connect("/ws") as ws:
        assert hello(ws, credential="wcid") == {
            "type": "error", "message": "invalid credential",
        }


def test_hello_accepts_a_minted_session_and_rejects_it_after_expiry(client):
    token = client.app_state.login.mint()
    with client.websocket_connect("/ws") as ws:
        assert hello(ws, credential=token)["type"] == "ready"

    for key in list(client.app_state.login._sessions):
        client.app_state.login._sessions[key] = time.time() - 1  # 24h later
    with client.websocket_connect("/ws") as ws:
        assert hello(ws, credential=token) == {
            "type": "error", "message": "invalid credential",
        }


def test_ready_declares_formats(client):
    with client.websocket_connect("/ws") as ws:
        ready = hello(ws)
        assert ready["type"] == "ready"
        assert ready["mic_format"]["sample_rate"] == 16000
        assert ready["tts_format"]["sample_rate"] == 24000


def test_history_replay_then_live_entries(client, fakes):
    fakes.bridge.history_entries = [
        UserText(text="what's next", ts="t1"),
        AssistantText(text="shipping the store", ts="t2"),
        ToolActivity(label="Bash: git status", ts="t3"),
        TurnEnd(ts="t4"),
    ]
    with client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        replay = [json.loads(ws.receive_text()) for _ in range(3)]  # TurnEnd is not chat
        assert [(f["type"], f["role"], f["text"], f["final"]) for f in replay] == [
            ("chat", "user", "what's next", True),
            ("chat", "assistant", "shipping the store", True),
            ("chat", "activity", "Bash: git status", True),
        ]
        assert [f["ts"] for f in replay] == ["t1", "t2", "t3"]

        fakes.bridge.push_entry(AssistantText(text="also: tests pass", ts="t5"))
        live = json.loads(ws.receive_text())
        assert live == {
            "type": "chat", "role": "assistant", "text": "also: tests pass",
            "ts": "t5", "final": True,
        }


def test_compact_end_pushes_compacted_not_chat(client, fakes):
    fakes.bridge.history_entries = [CompactEnd(ts="t0")]  # stale: replay skips it
    with client.websocket_connect("/ws") as ws:
        assert hello(ws)["type"] == "ready"
        fakes.bridge.push_entry(CompactEnd(ts="t1"))  # live: stops the spinner
        assert json.loads(ws.receive_text()) == {"type": "compacted"}


def test_text_input_flows_to_pipeline_and_bridge_turn(client, fakes):
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        frames, audio = run_turn(ws, "hello world")
        assert [f["type"] for f in frames] == ["state", "speech_end", "state"]
        assert frames[0]["state"] == "thinking"
        assert frames[2]["state"] == "listening"
        assert audio == b"\x01\x02"
        assert fakes.pipelines[-1].texts == ["hello world"]
        assert fakes.bridge.turns == ["hello world"]


def test_binary_feeds_pipeline_and_mute_routes(client, fakes):
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_bytes(b"\x00\x01")
        ws.send_text(Mute(muted=True).model_dump_json())
        ws.send_text(Hush().model_dump_json())
        run_turn(ws, "sync")  # round-trip so the frames above are processed
        pipeline = fakes.pipelines[-1]
        assert pipeline.fed == [b"\x00\x01"]
        assert pipeline.muted is True
        assert pipeline.hushes == 1


def test_second_connection_takes_over(client, fakes):
    with client.websocket_connect("/ws") as ws1:
        hello(ws1)
        with client.websocket_connect("/ws") as ws2:
            assert hello(ws2)["type"] == "ready"
            assert json.loads(ws1.receive_text()) == {
                "type": "error", "message": "another connection took over",
            }
            with pytest.raises(WebSocketDisconnect):
                ws1.receive_text()
            assert fakes.pipelines[0].closed
            run_turn(ws2, "still alive")  # the takeover connection is fully functional


class RecordingManager:
    """Stands in for WorkstreamManager to prove the WS wiring routes buttons."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.convo_transcript = Path("/transcripts/convo.jsonl")
        self.stored_convo_model = "fable"

    def convo_model(self) -> str:
        return self.stored_convo_model

    async def new_workstream(self) -> None:
        self.calls.append(("new_workstream",))

    async def send_to_workstream(self, name: str) -> None:
        self.calls.append(("send_to_workstream", name))

    async def compact_workstream(self, name: str) -> None:
        self.calls.append(("compact_workstream", name))

    def set_model(self, target: str, model: str) -> None:
        self.calls.append(("set_model", target, model))

    async def push_cards(self) -> None:
        self.calls.append(("push_cards",))

    def transcript_path(self, name: str) -> Path | None:
        return Path(f"/transcripts/{name}.jsonl") if name == "ws-known" else None


def test_buttons_reach_manager_compact_reaches_bridge(client, fakes):
    manager = RecordingManager()
    client.app_state.runtime.workstreams = manager
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(NewWorkstream().model_dump_json())
        ws.send_text(SendToWorkstream(workstream="ws-auth").model_dump_json())
        ws.send_text(Compact().model_dump_json())
        ws.send_text(CompactWorkstream(workstream="ws-auth").model_dump_json())
        ws.send_text(CheckIn(workstream="ws-known").model_dump_json())
        # check_in speaks through the pipeline: consume its turn frames
        assert json.loads(ws.receive_text())["state"] == "thinking"
        ws.receive_bytes()
        assert json.loads(ws.receive_text())["type"] == "speech_end"
        assert json.loads(ws.receive_text())["state"] == "listening"

        wait_for(lambda: len(manager.calls) == 3)
        assert manager.calls == [
            ("new_workstream",),
            ("send_to_workstream", "ws-auth"),
            ("compact_workstream", "ws-auth"),
        ]
        assert fakes.bridge.slashes == ["/compact"]
        directive = fakes.pipelines[-1].texts[-1]
        assert directive == (
            "Check in on workstream ws-known: read the tail of"
            " /transcripts/ws-known.jsonl and tell me where things stand."
        )


def test_set_model_persists_and_convo_switches_live(client, fakes):
    manager = RecordingManager()
    client.app_state.runtime.workstreams = manager
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(SetModel(target="convo", model="sonnet").model_dump_json())
        ws.send_text(SetModel(target="workstream", model="opus").model_dump_json())
        wait_for(lambda: manager.calls.count(("push_cards",)) == 2)
    assert manager.calls == [
        ("set_model", "convo", "sonnet"),
        ("push_cards",),
        ("set_model", "workstream", "opus"),
        ("push_cards",),  # workstream picks apply at spawn: no slash, just persisted
    ]
    assert fakes.bridge.slashes == ["/model sonnet"]  # only the convo session switches live


def test_set_model_engine_switch_clears_the_convo(client, fakes):
    manager = RecordingManager()  # current convo model: fable (claude)
    client.app_state.runtime.workstreams = manager
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(SetModel(target="convo", model="sol").model_dump_json())
        # A fresh session on the new engine, announced like the Clear button.
        assert json.loads(ws.receive_text()) == {"type": "convo_cleared"}
        assert fakes.convo_resets == 1
        assert manager.convo_transcript == fakes.fresh_transcript
    assert fakes.bridge.slashes == []  # /model can't cross engines
    assert ("set_model", "convo", "sol") in manager.calls


def test_set_model_same_pick_changes_nothing_live(client, fakes):
    manager = RecordingManager()
    client.app_state.runtime.workstreams = manager
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(SetModel(target="convo", model="fable").model_dump_json())
        wait_for(lambda: ("push_cards",) in manager.calls)
    assert fakes.bridge.slashes == []
    assert fakes.convo_resets == 0


def test_clear_convo_resets_and_repoints_the_manager(client, fakes):
    manager = RecordingManager()
    client.app_state.runtime.workstreams = manager
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(ClearConvo().model_dump_json())
        assert json.loads(ws.receive_text()) == {"type": "convo_cleared"}
        assert fakes.convo_resets == 1
        assert manager.convo_transcript == fakes.fresh_transcript


def test_check_in_unknown_workstream_pushes_error(client, fakes):
    client.app_state.runtime.workstreams = RecordingManager()
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        ws.send_text(CheckIn(workstream="ws-missing").model_dump_json())
        assert json.loads(ws.receive_text()) == {
            "type": "error", "message": "unknown workstream: ws-missing",
        }


def test_approval_round_trips_ws_and_http(client):
    with client.websocket_connect("/ws") as ws:
        hello(ws)
        result = {}

        def post():
            result["response"] = client.post(
                "/approvals",
                json={
                    "session_id": "s1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf /tmp/x"},
                },
                headers={"X-Workstreams-Token": "boot-token"},
            )

        thread = threading.Thread(target=post)
        thread.start()
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "approval_request"
        assert frame["session"] == "s1"
        assert frame["tool"] == "Bash"
        assert frame["summary"] == "rm -rf /tmp/x"
        ws.send_text(
            Approval(approval_id=frame["approval_id"], approved=True).model_dump_json()
        )
        thread.join(timeout=5)
        assert result["response"].json() == {"decision": "allow"}


async def test_protocol_sink_maps_pipeline_output():
    conn = FakeConn()
    sink = ProtocolSink(conn)
    await sink.state(PipelineState.THINKING)
    await sink.transcript("user", "partial wor", False)  # STT interim
    await sink.audio(b"\x00\x01")
    await sink.speech_end()
    state, chat, end = conn.messages
    assert state.state == "thinking"
    assert (chat.role, chat.text, chat.final) == ("user", "partial wor", False)
    assert chat.ts  # interims are stamped server-side
    assert end.type == "speech_end"
    assert conn.audio == [b"\x00\x01"]
