from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_check_reads_provider_configuration_from_installed_plist(tmp_path: Path) -> None:
    launch_agents = tmp_path / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.remote-workstreams.server.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>EnvironmentVariables</key><dict>
    <key>REMOTE_WORKSTREAMS_PORT</key><string>1</string>
    <key>REMOTE_WORKSTREAMS_STT_PROVIDER</key><string>moonshine</string>
    <key>REMOTE_WORKSTREAMS_TTS_PROVIDER</key><string>moonshine</string>
    <key>REMOTE_WORKSTREAMS_MOONSHINE_MODEL_DIR</key><string>/models/local</string>
  </dict>
</dict></plist>
"""
    )
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    for name in (
        "REMOTE_WORKSTREAMS_PORT",
        "REMOTE_WORKSTREAMS_STT_PROVIDER",
        "REMOTE_WORKSTREAMS_TTS_PROVIDER",
        "REMOTE_WORKSTREAMS_MOONSHINE_MODEL_DIR",
    ):
        env.pop(name, None)

    result = subprocess.run(
        ["bash", str(root / "skills/deploy-rw/scripts/check.sh"), str(root)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "stt_provider=moonshine" in result.stdout
    assert "tts_provider=moonshine" in result.stdout
    assert "moonshine_model_dir=/models/local" in result.stdout
    assert "secret_deepgram-api-key=not-required" in result.stdout
    assert "secret_cartesia-api-key=not-required" in result.stdout
