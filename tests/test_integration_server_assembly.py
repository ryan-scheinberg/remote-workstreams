"""The assembled server: create_app DI with the REAL ConversationEngine and REAL
AudioPipeline, fake STT/TTS/execution. Drives the dispatch chain end-to-end over a
real WebSocket: dispatch #1 -> start(), #2 -> send(), events flow back through
pipeline.on_events AND out as protocol Event frames.

Adopted from the first-pass QA probes (2026-07-02).
"""

import asyncio
import json

from server_fakes import FakeExecution, FakeSTT, FakeTTS
from starlette.testclient import TestClient
from test_conversation import FakeClient

from voicecode.audio.pipeline import AudioPipeline
from voicecode.config import Config
from voicecode.engine.conversation import ConversationEngine
from voicecode.server.app import create_app
from voicecode.server.auth import hash_secret


class RealFakes:
    """Real engine (mock Anthropic client) + real pipeline; fake STT/TTS/execution."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.engines: list[ConversationEngine] = []
        self.clients: list[FakeClient] = []
        self.executions: list[FakeExecution] = []
        self.pipelines: list[AudioPipeline] = []

    def engine_factory(self) -> ConversationEngine:
        client = FakeClient(list(self.replies))
        engine = ConversationEngine(client)
        self.clients.append(client)
        self.engines.append(engine)
        return engine

    def execution_factory(self) -> FakeExecution:
        execution = FakeExecution()
        self.executions.append(execution)
        return execution

    def stt_factory(self) -> FakeSTT:
        return FakeSTT()

    def tts_factory(self) -> FakeTTS:
        return FakeTTS()

    def pipeline_factory(self, stt, tts, engine, sink, on_dispatch=None) -> AudioPipeline:
        pipeline = AudioPipeline(stt, tts, engine, sink, on_dispatch=on_dispatch)
        self.pipelines.append(pipeline)
        return pipeline


REPLIES = [
    "On it, kicking that off right now.<dispatch>fix the failing tests</dispatch>",
    "Sure, adding the docs to the queue.<dispatch>update the docs too</dispatch>",
    "Still working through it, nothing new yet.",
]


def make(tmp_path):
    fakes = RealFakes(REPLIES)
    config = Config(data_dir=tmp_path / "data")
    app = create_app(
        config,
        engine_factory=fakes.engine_factory,
        execution_factory=fakes.execution_factory,
        stt_factory=fakes.stt_factory,
        tts_factory=fakes.tts_factory,
        pipeline_factory=fakes.pipeline_factory,
    )
    return app, fakes


def hello(ws, credential="cred-1", session_id=None) -> tuple[dict, dict]:
    ws.send_text(json.dumps({"type": "hello", "credential": credential, "session_id": session_id}))
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "ready", ready
    sessions = json.loads(ws.receive_text())
    assert sessions["type"] == "sessions", sessions
    return ready, sessions


def recv_until(ws, msg_type: str, limit: int = 20) -> tuple[dict, list[dict]]:
    seen = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        seen.append(msg)
        if msg["type"] == msg_type:
            return msg, seen
    raise AssertionError(f"never saw {msg_type}; got {seen}")


def test_dispatch_chain_end_to_end(tmp_path):
    app, fakes = make(tmp_path)
    with TestClient(app) as client:
        store = app.state.store
        store.create_credential("dev", hash_secret("cred-1"))
        with client.websocket_connect("/ws") as ws:
            ready, sessions = hello(ws)
            assert len(sessions["sessions"]) == 1  # 0-session store: auto-created

            # --- turn 1: dispatch -> start() ---
            ws.send_text(json.dumps({"type": "text_input", "text": "please fix the tests"}))
            event, seen = recv_until(ws, "event")
            assert event["event"]["type"] == "task_started"
            for m in seen:  # spoken text never contains the dispatch tag
                if m["type"] == "transcript":
                    assert "dispatch" not in m["text"], m
            finals = [m for m in seen if m["type"] == "transcript" and m["final"]]
            assert ("assistant", "On it, kicking that off right now.") in [
                (m["role"], m["text"]) for m in finals
            ]
            execution = fakes.executions[0]
            assert execution.started_prompts == ["fix the failing tests"]
            assert execution.sent == []

            # --- turn 2: dispatch -> send() ---
            ws.send_text(json.dumps({"type": "text_input", "text": "also update the docs"}))
            event, seen = recv_until(ws, "event")
            assert event["event"]["type"] == "progress"  # FakeExecution.send -> Progress
            assert execution.started_prompts == ["fix the failing tests"]
            assert execution.sent == ["update the docs too"]

            # --- turn 3: the injected events reached the REAL engine's prompt ---
            ws.send_text(json.dumps({"type": "text_input", "text": "how is it going?"}))
            recv_until(ws, "state")  # thinking
            _, seen = recv_until(ws, "state")  # back to listening after the turn
            engine_client = fakes.clients[0]
            # task_started drained into turn 2's prompt; progress into turn 3's
            second_prompt = engine_client.calls[1]["messages"][-1]["content"]
            assert second_prompt.startswith("<system-reminder>"), second_prompt[:80]
            assert "- [task_started] Execution started." in second_prompt
            third_prompt = engine_client.calls[2]["messages"][-1]["content"]
            assert "- [progress] Still working." in third_prompt
            assert third_prompt.endswith("how is it going?")
            roles = [m["role"] for m in fakes.engines[0].messages]
            assert roles == ["user", "assistant"] * 3, roles

            # --- approval for an unknown gate_id: must not blow up the socket ---
            ws.send_text(json.dumps({"type": "approval", "gate_id": "no-such", "approved": True}))
            # --- switch to the CURRENT session id ---
            ws.send_text(json.dumps({"type": "switch_session", "session_id": ready["session_id"]}))
            msg, seen = recv_until(ws, "ready")
            assert not any(m["type"] == "error" for m in seen), seen
            assert msg["session_id"] == ready["session_id"]
            sessions2 = json.loads(ws.receive_text())
            assert sessions2["type"] == "sessions"
            assert len(fakes.engines) == 1  # runtime + engine reused, history intact
            assert execution.approvals == [("no-such", True)]

            # --- binary flood on the live socket while nothing is wrong ---
            for _ in range(200):
                ws.send_bytes(b"\x01\x02" * 320)
            ws.send_text(json.dumps({"type": "text_input", "text": "still alive?"}))
            _, seen = recv_until(ws, "state", limit=30)
            assert not any(m["type"] == "error" for m in seen), seen

        # --- persistence: committed turns were saved through the REAL engine ---
        row = store.get_session(ready["session_id"])
        assert row is not None
        saved_roles = [m["role"] for m in row.messages]
        assert saved_roles[:6] == ["user", "assistant"] * 3, saved_roles
        assert "<dispatch>" in row.messages[1]["content"]  # raw reply persisted


def test_unknown_gate_on_real_adapter_is_noop():
    """The concrete ClaudeCodeAdapter must ignore approvals for unknown gates."""
    from voicecode.adapters.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter(Config())
    asyncio.run(adapter.approve("no-such-gate", True))  # must not raise
