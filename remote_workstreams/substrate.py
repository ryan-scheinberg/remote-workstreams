"""tmux substrate: spawn and drive real interactive Claude Code / Codex sessions.

The only module that shells out to tmux. Writing goes through send-keys /
paste-buffer; reading replies never happens here — transcripts are tailed by
remote_workstreams.transcript. capture() exists only for the raw-terminal view.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import shlex
import struct
import termios
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Codex writes its rollout file at TUI boot; spawn polls the sessions dir for it.
_CODEX_POLL_S = 0.5
_CODEX_BOOT_BUDGET_S = 60.0
_ROLLOUT_ID = re.compile(r"-([0-9a-f-]{36})\.jsonl$")

# tmux enables the Kitty keyboard protocol on capable attached terminals.  Its
# send-keys/paste-buffer commands bypass that client-side translation, which
# makes recent Claude Code builds receive literal escape codes and ignore
# Enter.  Keep one tiny, invisible terminal client per window and write through
# it exactly as a real terminal would.
_KITTY_ENTER = b"\x1b[13;1u"
_CLIENT_ROWS = 50
_CLIENT_COLS = 220
_CLIENT_READY_S = 0.25
_TERMINAL_RESPONSES = (
    (b"\x1b[c", b"\x1b[?1;2c"),
    (b"\x1b[>c", b"\x1b[>0;370;0c"),
    (b"\x1b[>q", b"\x1bP>|XTerm(370)\x1b\\"),
    (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\"),
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:0000/0000/0000\x1b\\"),
    (b"\x1b[18t", b"\x1b[8;50;220t"),
    (b"\x1b[14t", b"\x1b[4;1000;1760t"),
    (b"\x1b[?2026$p", b"\x1b[?2026;2$y"),
)


def slug(path: Path | str) -> str:
    """Claude Code's project-dir slug: /Users/alice -> -Users-alice."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@dataclass(frozen=True)
class SessionSpec:
    name: str  # tmux window name, e.g. "convo", "ws-auth"
    model: str  # e.g. "fable", "opus"
    effort: str  # "low" / "high" / "xhigh"
    display_name: str
    engine: str = "claude"  # "claude" or "codex"; the fields below are claude-only
    settings_file: Path | None = None
    plugin_dir: Path | None = None
    initial_prompt: str | None = None  # e.g. "/remote-workstreams:role-root", trailing CLI arg
    resume: bool = False  # resume an existing session id instead of minting one
    remote_control: bool = False  # visible/drivable from the iOS Claude app


@dataclass
class CCSession:
    session_id: str
    window: str  # e.g. "voice:convo"
    transcript: Path
    spec: SessionSpec


@dataclass
class _TerminalClient:
    process: asyncio.subprocess.Process
    fd: int
    waiter: asyncio.Task[None] | None = None
    buffer: bytes = b""


class Tmux:
    """Thin async wrapper over the tmux CLI."""

    def __init__(self) -> None:
        self._clients: dict[str, _TerminalClient] = {}

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
        # Let tmux consume focus events from the invisible terminal clients;
        # otherwise a raw CSI I can land in Claude's composer on attach.
        await self._run("set-option", "-g", "focus-events", "on")

    async def new_window(self, session: str, name: str, cwd: Path) -> None:
        # -d keeps existing per-window terminal clients on their own windows.
        await self._run("new-window", "-d", "-t", session, "-n", name, "-c", str(cwd))

    async def _ensure_client(self, window: str) -> _TerminalClient:
        current = self._clients.get(window)
        if current is not None and current.process.returncode is None:
            return current

        master, slave = pty.openpty()
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", _CLIENT_ROWS, _CLIENT_COLS, 0, 0),
        )
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux",
                "attach-session",
                "-f",
                "ignore-size",
                "-t",
                window,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
        finally:
            os.close(slave)

        loop = asyncio.get_running_loop()
        client = _TerminalClient(process=process, fd=master)
        self._clients[window] = client
        loop.add_reader(master, self._drain_client, window)
        client.waiter = asyncio.create_task(self._reap_client(window, client))
        await asyncio.sleep(_CLIENT_READY_S)
        return client

    def _drain_client(self, window: str) -> None:
        client = self._clients.get(window)
        if client is None:
            return
        try:
            chunk = os.read(client.fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(client.fd)
            return
        client.buffer = (client.buffer + chunk)[-4096:]
        for query, response in _TERMINAL_RESPONSES:
            if query not in client.buffer:
                continue
            try:
                os.write(client.fd, response)
            except OSError:
                return
            client.buffer = client.buffer.replace(query, b"", 1)

    async def _reap_client(self, window: str, client: _TerminalClient) -> None:
        await client.process.wait()
        loop = asyncio.get_running_loop()
        loop.remove_reader(client.fd)
        try:
            os.close(client.fd)
        except OSError:
            pass
        if self._clients.get(window) is client:
            self._clients.pop(window, None)

    async def _write_client(self, window: str, data: bytes) -> None:
        client = await self._ensure_client(window)
        os.write(client.fd, data)

    async def send_line(self, window: str, text: str) -> None:
        await self._write_client(window, text.encode() + _KITTY_ENTER)

    async def paste(self, window: str, text: str) -> None:
        """Multiline-safe inject through a real tmux terminal client."""
        await self._write_client(window, b"\x1b[200~" + text.encode() + b"\x1b[201~")
        # The TUI needs a beat to ingest the paste before Enter submits it.
        await asyncio.sleep(0.5)
        await self._write_client(window, _KITTY_ENTER)

    async def send_key(self, window: str, key: str) -> None:
        if key != "Enter":
            raise ValueError(f"unsupported terminal key: {key}")
        await self._write_client(window, _KITTY_ENTER)

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
    """Claude Code / Codex sessions as tmux windows: spawn, inject, check, kill."""

    def __init__(
        self,
        tmux: Tmux,
        home: Path,
        tmux_session: str = "voice",
        codex_command: str = "codex",
    ) -> None:
        self._tmux = tmux
        self._home = home
        self._session = tmux_session
        self._codex_command = codex_command

    @property
    def transcript_dir(self) -> Path:
        return self._home / ".claude/projects" / slug(self._home)

    def codex_transcript(self, session_id: str) -> Path:
        """The rollout file for a codex session id — its filename carries a
        creation timestamp, so it can only be found, not derived."""
        sessions = self._home / ".codex/sessions"
        hits = sorted(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        return hits[-1] if hits else sessions / f"{session_id}.jsonl"

    async def spawn(self, spec: SessionSpec, session_id: str | None = None) -> CCSession:
        if spec.engine == "codex":
            return await self._spawn_codex(spec)
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
            # Role vocabulary uses xhigh for both engines. Claude Code names
            # that same top effort tier "max"; Codex accepts xhigh directly.
            "max" if spec.effort == "xhigh" else spec.effort,
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

    async def _spawn_codex(self, spec: SessionSpec) -> CCSession:
        """Codex mints its own session id and writes a rollout file at boot —
        spawn watches the sessions dir for the new file to learn both."""
        if spec.resume:
            raise ValueError("codex resume is not wired; spawn fresh instead")
        await self._tmux.ensure_session(self._session)
        await self._tmux.new_window(self._session, spec.name, self._home)
        window = f"{self._session}:{spec.name}"
        argv = [
            self._codex_command,
            "--model",
            spec.model,
            "--config",
            f'model_reasoning_effort="{spec.effort}"',
            # No phone-approval relay exists for codex; run autonomously inside
            # the write sandbox instead of stalling on unanswerable prompts.
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--config",
            "sandbox_workspace_write.network_access=true",
        ]
        if spec.initial_prompt is not None:
            argv.append(spec.initial_prompt)
        before = set(self._rollouts())
        await self._tmux.send_line(window, "command " + shlex.join(argv))
        transcript = await self._await_rollout(before)
        return CCSession(
            session_id=_ROLLOUT_ID.search(transcript.name).group(1),
            window=window,
            transcript=transcript,
            spec=spec,
        )

    def _rollouts(self) -> list[Path]:
        files = (self._home / ".codex/sessions").glob("*/*/*/rollout-*.jsonl")
        return [path for path in files if _ROLLOUT_ID.search(path.name)]

    async def _await_rollout(self, before: set[Path]) -> Path:
        deadline = time.monotonic() + _CODEX_BOOT_BUDGET_S
        while True:
            new = [path for path in self._rollouts() if path not in before]
            if new:
                return max(new, key=lambda path: path.stat().st_mtime)
            if time.monotonic() >= deadline:
                raise TimeoutError("codex wrote no rollout file after launch")
            await asyncio.sleep(_CODEX_POLL_S)

    async def send(self, session: CCSession, text: str) -> None:
        await self._tmux.paste(session.window, text)

    async def slash(self, session: CCSession, command: str) -> None:
        await self._tmux.type_line(session.window, command)
        if command.startswith("/model"):
            # CC 2.1.202 silently swallows the FIRST input submitted after /model
            # (typed or pasted); a blank Enter takes the hit so real input never does.
            await asyncio.sleep(1.0)
            await self._tmux.send_key(session.window, "Enter")

    async def rename(self, session: CCSession, name: str) -> None:
        """Give a Codex thread the same human name as its workstream."""
        if session.spec.engine == "codex":
            await self.slash(session, f"/rename {name}")

    async def archive(self, session: CCSession) -> None:
        """Archive a finished Codex thread from its ChatGPT/Codex history."""
        if session.spec.engine != "codex":
            return
        proc = await asyncio.create_subprocess_exec(
            self._codex_command,
            "archive",
            session.session_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode:
            detail = stderr.decode().strip() or "codex archive failed"
            raise RuntimeError(detail)

    async def alive(self, session: CCSession) -> bool:
        return await self._tmux.window_exists(session.window)

    async def kill(self, session: CCSession) -> None:
        await self._tmux.kill_window(session.window)
