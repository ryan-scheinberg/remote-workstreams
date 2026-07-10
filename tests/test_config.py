from remote_workstreams.config import Config


def test_codex_command_uses_the_environment_override(monkeypatch):
    monkeypatch.setenv(
        "REMOTE_WORKSTREAMS_CODEX_COMMAND", "/Applications/ChatGPT.app/Contents/Resources/codex"
    )

    assert Config.load().codex_command == "/Applications/ChatGPT.app/Contents/Resources/codex"


def test_local_voice_provider_configuration(monkeypatch):
    monkeypatch.setenv("REMOTE_WORKSTREAMS_STT_PROVIDER", "Moonshine")
    monkeypatch.setenv("REMOTE_WORKSTREAMS_TTS_PROVIDER", "moonshine")
    monkeypatch.setenv("REMOTE_WORKSTREAMS_MOONSHINE_TTS_SPEED", "1.15")

    config = Config.load()

    assert config.stt_provider == "moonshine"
    assert config.tts_provider == "moonshine"
    assert config.moonshine_tts_speed == 1.15


def test_config_rejects_unknown_voice_provider():
    try:
        Config(stt_provider="unknown")
    except ValueError as exc:
        assert "deepgram" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("unknown provider should be rejected")


def test_config_rejects_provider_for_the_wrong_voice_direction():
    for kwargs, expected in (
        ({"stt_provider": "cartesia"}, "stt_provider"),
        ({"tts_provider": "deepgram"}, "tts_provider"),
    ):
        try:
            Config(**kwargs)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("provider should be rejected")
