"""tmux substrate: spawn and drive real interactive Claude Code sessions.

The only module that shells out to tmux. Writing goes through send-keys /
paste-buffer; reading replies never happens here — transcripts are tailed by
voicecode.transcript. capture() exists only for the raw-terminal view.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path


def slug(path: Path | str) -> str:
    """Claude Code's project-dir slug: /Users/ryanscheinberg -> -Users-ryanscheinberg."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@dataclass(frozen=True)
class SessionSpec:
    name: str  # tmux window name, e.g. "convo", "ws-auth"
    model: str  # e.g. "fable", "opus"
    effort: str  # "low" / "high" / "xhigh"
    display_name: str
    settings_file: Path | None = None
    plugin_dir: Path | None = None
    initial_prompt: str | None = None  # e.g. "/voice-code:role-root", trailing CLI arg
    resume: bool = False  # resume an existing session id instead of minting one
    remote_control: bool = False  # visible/drivable from the iOS Claude app


@dataclass
class CCSession:
    session_id: str
    window: str  # e.g. "voice:convo"
    transcript: Path
    spec: SessionSpec


class Tmux:
    """Thin async wrapper over the tmux CLI."""

    async def _run(self, *args: str, stdin: bytes | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate(stdin)
        return proc.returncode or 0, out.decode()

    async def ensure_session(self, name: str) -> None:
        code, _ = await self._run("has-session", "-t", name)
        if code != 0:
            # Detached sessions default to 80x24; give the TUI a real size.
            await self._run(
                "new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", str(Path.home())
            )

    async def new_window(self, session: str, name: str, cwd: Path) -> None:
        await self._run("new-window", "-t", session, "-n", name, "-c", str(cwd))

    async def send_line(self, window: str, text: str) -> None:
        await self._run("send-keys", "-t", window, "-l", text)
        await self._run("send-keys", "-t", window, "Enter")

    async def paste(self, window: str, text: str) -> None:
        """Multiline-safe inject: bracketed paste, then Enter as its own keystroke."""
        await self._run("load-buffer", "-", stdin=text.encode())
        await self._run("paste-buffer", "-p", "-d", "-t", window)
        # The TUI needs a beat to ingest the paste before Enter submits it.
        await asyncio.sleep(0.5)
        await self._run("send-keys", "-t", window, "Enter")

    async def send_key(self, window: str, key: str) -> None:
        await self._run("send-keys", "-t", window, key)

    async def type_line(self, window: str, text: str) -> None:
        # Slash commands must be TYPED, not pasted, so the TUI's command mode triggers.
        await self.send_line(window, text)

    async def kill_window(self, window: str) -> None:
        await self._run("kill-window", "-t", window)

    async def window_exists(self, window: str) -> bool:
        code, _ = await self._run("list-panes", "-t", window)
        return code == 0

    async def capture(self, window: str) -> str:
        _, out = await self._run("capture-pane", "-p", "-t", window)
        return out

    async def list_windows(self, session: str) -> list[str]:
        _, out = await self._run("list-windows", "-t", session, "-F", "#{window_name}")
        return out.splitlines()


class Substrate:
    """Claude Code sessions as tmux windows: spawn, inject, check, kill."""

    def __init__(self, tmux: Tmux, home: Path, tmux_session: str = "voice") -> None:
        self._tmux = tmux
        self._home = home
        self._session = tmux_session

    @property
    def transcript_dir(self) -> Path:
        return self._home / ".claude/projects" / slug(self._home)

    async def spawn(self, spec: SessionSpec, session_id: str | None = None) -> CCSession:
        if spec.resume and session_id is None:
            raise ValueError("resume requires the existing session_id")
        session_id = session_id or str(uuid.uuid4())
        await self._tmux.ensure_session(self._session)
        await self._tmux.new_window(self._session, spec.name, self._home)
        window = f"{self._session}:{spec.name}"
        argv = [
            "claude",
            "--resume" if spec.resume else "--session-id",
            session_id,
            "--model",
            spec.model,
            "--effort",
            spec.effort,
            "-n",
            spec.display_name,
        ]
        if spec.remote_control:
            argv += ["--remote-control", spec.display_name]
        if spec.settings_file is not None:
            argv += ["--settings", str(spec.settings_file)]
        if spec.plugin_dir is not None:
            argv += ["--plugin-dir", str(spec.plugin_dir)]
        if spec.initial_prompt is not None:
            argv.append(spec.initial_prompt)
        # "command" bypasses the user's shell aliases/functions for bare `claude`.
        await self._tmux.send_line(window, "command " + shlex.join(argv))
        return CCSession(
            session_id=session_id,
            window=window,
            transcript=self.transcript_dir / f"{session_id}.jsonl",
            spec=spec,
        )

    async def send(self, session: CCSession, text: str) -> None:
        await self._tmux.paste(session.window, text)

    async def slash(self, session: CCSession, command: str) -> None:
        await self._tmux.type_line(session.window, command)

    async def alive(self, session: CCSession) -> bool:
        return await self._tmux.window_exists(session.window)

    async def kill(self, session: CCSession) -> None:
        await self._tmux.kill_window(session.window)
