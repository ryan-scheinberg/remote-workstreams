"""The brief's gated real-session test: real tmux, real Substrate, real ConvoBridge,
one real Claude Code session (haiku, cheap). Proves the live path the fake-tmux suite
cannot: spawn -> paste a turn -> sentences stream from the minted transcript path.

Run manually (spawns a real session on your Claude Code account):

    VOICECODE_LIVE=1 uv run pytest tests/test_live_convo.py

Uses tmux session "voice-qa" so it never collides with the real "voice" session;
the window and the tmux session are torn down in finally.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from voicecode.convo import ConvoBridge
from voicecode.substrate import SessionSpec, Substrate, Tmux

pytestmark = pytest.mark.skipif(
    os.environ.get("VOICECODE_LIVE") != "1",
    reason="live Claude Code session; set VOICECODE_LIVE=1 to run",
)

TMUX_SESSION = "voice-qa"
BOOT_TIMEOUT = 60.0
TURN_TIMEOUT = 90.0


async def _wait_for_tui(tmux: Tmux, window: str) -> None:
    """Wait until the Claude Code TUI owns the pane (banner rendered), so the
    pasted turn lands in the session and not in the shell that launched it."""
    capture = ""
    try:
        async with asyncio.timeout(BOOT_TIMEOUT):
            while "Claude Code" not in capture and "shortcuts" not in capture:
                await asyncio.sleep(0.5)
                capture = await tmux.capture(window)
    except TimeoutError:
        pytest.fail(f"Claude Code TUI never appeared in {window}; pane:\n{capture}")
    await asyncio.sleep(2)  # let the input box finish initializing


async def _kill_tmux_session(name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def test_live_turn_streams_sentences_from_real_session():
    tmux = Tmux()
    substrate = Substrate(tmux, Path.home(), tmux_session=TMUX_SESSION)
    spec = SessionSpec(
        name="convo", model="haiku", effort="low", display_name="voice-qa-convo"
    )
    try:
        session = await substrate.spawn(spec)
        bridge = ConvoBridge(substrate, session)
        run = asyncio.create_task(bridge.run())
        try:
            await _wait_for_tui(tmux, session.window)
            sentences: list[str] = []
            async with asyncio.timeout(TURN_TIMEOUT):
                turn = bridge.turn("Reply with exactly the single word: pineapple")
                async for sentence in turn:
                    sentences.append(sentence)
            assert "pineapple" in " ".join(sentences).lower(), f"sentences: {sentences}"
            assert session.transcript.exists()  # the minted --session-id path is real
        finally:
            await bridge.close()
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run
            with contextlib.suppress(Exception):
                await substrate.kill(session)
    finally:
        await _kill_tmux_session(TMUX_SESSION)
