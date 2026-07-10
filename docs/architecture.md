# Architecture

```text
iPhone Safari PWA
        │ HTTPS/WebSocket over Tailscale
        ▼
FastAPI + launchd on Mac
  ├─ WebAuthn pairing/login and approval relay
  ├─ AudioPipeline: PCM → STT → conversation → TTS → PCM
  ├─ Claude Code or Codex CLI sessions inside tmux
  ├─ JSONL transcript tailers (chat/workstream source of truth)
  ├─ SQLite control store
  └─ static PWA
```

The composition root in `remote_workstreams/server/__main__.py` chooses concrete adapters from configuration. `MoonshineSTT` runs blocking native inference on a worker thread and publishes partial/final `TranscriptChunk` objects into the async pipeline. `MoonshineTTS` runs synthesis off the event loop, converts float samples to signed PCM, and resamples to the protocol's 24 kHz output.

The pipeline owns endpoint grace, barge-in, hush, echo suppression, and sentence chunking. A provider adapter must therefore obey the small `STTAdapter`/`TTSAdapter` contracts rather than reaching into WebSocket or agent code.
