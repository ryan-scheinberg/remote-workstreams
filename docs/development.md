# Development

```bash
uv sync --extra local-voice
uv run ruff check remote_workstreams tests
uv run python -m pytest
```

The full suite is the release gate. `tests/test_moonshine_stt.py` and `tests/test_moonshine_tts.py` use fakes so CI does not download models. The live local check is opt-in and uses the cached model files:

```bash
REMOTE_WORKSTREAMS_STT_PROVIDER=moonshine \
REMOTE_WORKSTREAMS_TTS_PROVIDER=moonshine \
uv run python -m remote_workstreams.audio.roundtrip
```

Keep provider code async-safe: native/model calls belong in `asyncio.to_thread` or a worker thread, and cancellation must not leak late audio into a new sentence. Add a fake-based regression test for every adapter behavior before running a live smoke test.
