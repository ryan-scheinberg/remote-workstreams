"""hooks/ask_phone.py run as a real subprocess against a threaded stub HTTP server."""

import http.server
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "ask_phone.py"

BASH_PAYLOAD = {
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "sudo rm -rf /tmp/x"},
}


class StubHandler(http.server.BaseHTTPRequestHandler):
    decision = "allow"
    requests: list[tuple[str, dict, dict]] = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        StubHandler.requests.append((self.path, dict(self.headers), body))
        response = json.dumps({"decision": StubHandler.decision}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_port():
    StubHandler.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def run_hook(payload: dict, port: int, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--port", str(port), "--token", "tok-1", *flags],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_allow_prints_permission_decision(stub_port):
    StubHandler.decision = "allow"
    result = run_hook(BASH_PAYLOAD, stub_port)
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "allow"
    assert output["permissionDecisionReason"] == "voice-code phone approval"
    (path, headers, body) = StubHandler.requests[0]
    assert path == "/approvals"
    assert headers["X-Voicecode-Token"] == "tok-1"
    assert body == BASH_PAYLOAD  # the raw hook JSON is relayed untouched


def test_deny_prints_deny(stub_port):
    StubHandler.decision = "deny"
    result = run_hook(BASH_PAYLOAD, stub_port)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gate_bash_skips_safe_command(stub_port):
    payload = dict(BASH_PAYLOAD, tool_input={"command": "git status && ls -la"})
    result = run_hook(payload, stub_port, "--gate-bash")
    assert result.returncode == 0
    assert result.stdout == ""  # silence: the tool call proceeds natively
    assert StubHandler.requests == []


def test_gate_bash_skips_non_bash_tools(stub_port):
    payload = {"session_id": "s1", "tool_name": "Write", "tool_input": {"file_path": "/x"}}
    result = run_hook(payload, stub_port, "--gate-bash")
    assert result.returncode == 0
    assert result.stdout == ""
    assert StubHandler.requests == []


@pytest.mark.parametrize(
    "command",
    [
        "sudo launchctl load foo",
        "rm -rf /tmp/x",
        "git push --force origin main",
        "git push -f",
        "git reset --hard HEAD~3",
        "git branch -D feature",
        "git clean -fd",
        "kill -9 1234",
    ],
)
def test_gate_bash_relays_destructive_commands(stub_port, command):
    StubHandler.decision = "deny"
    payload = dict(BASH_PAYLOAD, tool_input={"command": command})
    result = run_hook(payload, stub_port, "--gate-bash")
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(StubHandler.requests) == 1


def test_server_down_prints_nothing_and_exits_zero():
    # bind-then-close to get a port with nothing listening
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    result = run_hook(BASH_PAYLOAD, port, "--wait", "2")
    assert result.returncode == 0
    assert result.stdout == ""
