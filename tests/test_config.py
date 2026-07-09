from remote_workstreams.config import Config


def test_codex_command_uses_the_environment_override(monkeypatch):
    monkeypatch.setenv(
        "REMOTE_WORKSTREAMS_CODEX_COMMAND", "/Applications/ChatGPT.app/Contents/Resources/codex"
    )

    assert Config.load().codex_command == "/Applications/ChatGPT.app/Contents/Resources/codex"
