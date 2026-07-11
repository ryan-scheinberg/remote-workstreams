import asyncio
import uuid
from pathlib import Path

import pytest

from remote_workstreams import substrate as substrate_module
from remote_workstreams.substrate import CCSession, SessionSpec, Substrate, Tmux, slug

HOME = Path("/Users/alice")

CONVO = SessionSpec(name="convo", model="fable", effort="low", display_name="convo")


class FakeTmux:
    """Duck-typed tmux double: records every call; window_exists is scripted."""

    def __init__(self, existing_windows: set[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self.existing_windows = existing_windows if existing_windows is not None else set()

    async def ensure_session(self, name: str) -> None:
        self.calls.append(("ensure_session", name))

    async def new_window(self, session: str, name: str, cwd: Path) -> None:
        self.calls.append(("new_window", session, name, cwd))

    async def send_line(self, window: str, text: str) -> None:
        self.calls.append(("send_line", window, text))

    async def paste(self, window: str, text: str) -> None:
        self.calls.append(("paste", window, text))

    async def type_line(self, window: str, text: str) -> None:
        self.calls.append(("type_line", window, text))

    async def send_key(self, window: str, key: str) -> None:
        self.calls.append(("send_key", window, key))

    async def kill_window(self, window: str) -> None:
        self.calls.append(("kill_window", window))

    async def window_exists(self, window: str) -> bool:
        self.calls.append(("window_exists", window))
        return window in self.existing_windows


class RecordingTmux(Tmux):
    """Real Tmux with the subprocess layer replaced; has-session exit code scripted."""

    def __init__(self, has_session_code: int = 0) -> None:
        self.runs: list[tuple] = []
        self.has_session_code = has_session_code

    async def _run(self, *args: str, stdin: bytes | None = None) -> tuple[int, str]:
        self.runs.append((args, stdin))
        if args[0] == "has-session":
            return self.has_session_code, ""
        return 0, ""


def test_slug():
    assert slug("/Users/alice") == "-Users-alice"


def test_transcript_dir():
    sub = Substrate(FakeTmux(), home=HOME)
    expected = Path("/Users/alice/.claude/projects/-Users-alice")
    assert sub.transcript_dir == expected


async def test_spawn_full_options():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    spec = SessionSpec(
        name="ws-auth",
        model="fable",
        effort="xhigh",
        display_name="Wire the auth flow",
        settings_file=Path("/tmp/ws-settings.json"),
        plugin_dir=Path("/Users/alice/plugins/remote-workstreams"),
        initial_prompt="/remote-workstreams:role-root plan in stint-3.md",
        remote_control=True,
    )
    session = await sub.spawn(spec)

    uuid.UUID(session.session_id)  # a real minted uuid
    assert session.window == "voice:ws-auth"
    assert session.transcript == sub.transcript_dir / f"{session.session_id}.jsonl"
    assert session.spec is spec

    expected = (
        f"command claude --session-id {session.session_id} --model fable --effort xhigh"
        " -n 'Wire the auth flow' --remote-control 'Wire the auth flow'"
        " --settings /tmp/ws-settings.json"
        " --plugin-dir /Users/alice/plugins/remote-workstreams"
        " '/remote-workstreams:role-root plan in stint-3.md'"
    )
    assert fake.calls == [
        ("ensure_session", "voice"),
        ("new_window", "voice", "ws-auth", HOME),
        ("send_line", "voice:ws-auth", expected),
    ]


async def test_spawn_minimal():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    session = await sub.spawn(CONVO)

    expected = f"command claude --session-id {session.session_id} --model fable --effort low -n convo"
    assert fake.calls[-1] == ("send_line", "voice:convo", expected)


async def test_spawn_resume_uses_resume_flag():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    spec = SessionSpec(name="convo", model="fable", effort="low", display_name="convo", resume=True)
    sid = "11111111-2222-3333-4444-555555555555"
    session = await sub.spawn(spec, session_id=sid)

    expected = f"command claude --resume {sid} --model fable --effort low -n convo"
    assert fake.calls[-1] == ("send_line", "voice:convo", expected)
    assert session.session_id == sid
    assert session.transcript == sub.transcript_dir / f"{sid}.jsonl"


async def test_spawn_resume_requires_session_id():
    sub = Substrate(FakeTmux(), home=HOME)
    spec = SessionSpec(name="convo", model="fable", effort="low", display_name="convo", resume=True)
    with pytest.raises(ValueError):
        await sub.spawn(spec)


CODEX_UUID = "019f3e26-683e-78a2-ae60-4e811e872382"
CODEX_ROLLOUT = f"rollout-2026-07-08T10-00-00-{CODEX_UUID}.jsonl"


def codex_spec(**overrides) -> SessionSpec:
    fields = dict(
        name="convo", model="gpt-5.6-sol", effort="low", display_name="convo",
        engine="codex", initial_prompt="$role-convo",
    )
    fields.update(overrides)
    return SessionSpec(**fields)


async def test_spawn_codex_discovers_the_rollout_file(tmp_path, monkeypatch):
    monkeypatch.setattr(substrate_module, "_CODEX_POLL_S", 0.01)
    fake = FakeTmux()
    sub = Substrate(
        fake,
        home=tmp_path,
        codex_command="/Applications/ChatGPT.app/Contents/Resources/codex",
    )
    day = tmp_path / ".codex/sessions/2026/07/08"
    day.mkdir(parents=True)
    (day / "rollout-2026-07-08T09-00-00-00000000-0000-0000-0000-000000000000.jsonl").touch()

    task = asyncio.create_task(sub.spawn(codex_spec()))
    await asyncio.sleep(0.03)  # spawn is polling; only the pre-existing file is there
    rollout = day / CODEX_ROLLOUT
    rollout.touch()
    session = await task

    assert session.session_id == CODEX_UUID  # codex minted it; read off the filename
    assert session.transcript == rollout
    assert session.window == "voice:convo"
    expected = (
        "command /Applications/ChatGPT.app/Contents/Resources/codex --model gpt-5.6-sol"
        " --config 'model_reasoning_effort=\"low\"'"
        " --sandbox workspace-write --ask-for-approval never"
        " --config sandbox_workspace_write.network_access=true '$role-convo'"
    )
    assert fake.calls == [
        ("ensure_session", "voice"),
        ("new_window", "voice", "convo", tmp_path),
        ("send_line", "voice:convo", expected),
    ]


async def test_spawn_codex_times_out_without_a_rollout(tmp_path, monkeypatch):
    monkeypatch.setattr(substrate_module, "_CODEX_POLL_S", 0.01)
    monkeypatch.setattr(substrate_module, "_CODEX_BOOT_BUDGET_S", 0.03)
    sub = Substrate(FakeTmux(), home=tmp_path)
    with pytest.raises(TimeoutError):
        await sub.spawn(codex_spec())


async def test_spawn_codex_rejects_resume(tmp_path):
    sub = Substrate(FakeTmux(), home=tmp_path)
    with pytest.raises(ValueError):
        await sub.spawn(codex_spec(resume=True))


def test_codex_transcript_globs_by_session_id(tmp_path):
    sub = Substrate(FakeTmux(), home=tmp_path)
    missing = sub.codex_transcript(CODEX_UUID)
    assert not missing.exists()  # placeholder path; tails tolerate a missing file

    day = tmp_path / ".codex/sessions/2026/07/08"
    day.mkdir(parents=True)
    rollout = day / CODEX_ROLLOUT
    rollout.touch()
    assert sub.codex_transcript(CODEX_UUID) == rollout


async def test_send_pastes_into_window():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    session = await sub.spawn(CONVO)
    await sub.send(session, "line one\nline two `$HOME`")
    assert fake.calls[-1] == ("paste", "voice:convo", "line one\nline two `$HOME`")


async def test_slash_is_typed_not_pasted():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    session = await sub.spawn(CONVO)
    await sub.slash(session, "/compact")
    assert fake.calls[-1] == ("type_line", "voice:convo", "/compact")
    assert not [call for call in fake.calls if call[0] == "paste"]


async def test_slash_model_sends_a_sacrificial_enter():
    """CC 2.1.202 swallows the first input submitted after /model — verified live:
    a voice turn pasted 9s after /model never reached the session. The blank
    Enter absorbs the eat; other slash commands don't get (or need) it."""
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    session = await sub.spawn(CONVO)
    await sub.slash(session, "/model sonnet")
    assert fake.calls[-2:] == [
        ("type_line", "voice:convo", "/model sonnet"),
        ("send_key", "voice:convo", "Enter"),
    ]


async def test_rename_types_codex_slash_command():
    fake = FakeTmux()
    sub = Substrate(fake, home=HOME)
    spec = codex_spec()
    session = CCSession("codex-id", "voice:convo", HOME / "rollout.jsonl", spec)

    await sub.rename(session, "Wire the auth flow")

    assert fake.calls[-1] == ("type_line", "voice:convo", "/rename Wire the auth flow")


async def test_alive_and_kill():
    fake = FakeTmux(existing_windows={"voice:convo"})
    sub = Substrate(fake, home=HOME)
    session = await sub.spawn(CONVO)
    assert await sub.alive(session) is True
    fake.existing_windows.clear()
    assert await sub.alive(session) is False
    await sub.kill(session)
    assert fake.calls[-1] == ("kill_window", "voice:convo")


async def test_paste_sequence_order(monkeypatch):
    tmux = RecordingTmux()

    async def record_sleep(seconds: float) -> None:
        tmux.runs.append((("sleep", str(seconds)), None))

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    await tmux.paste("voice:convo", 'a\nb `x` "q" $HOME')

    assert tmux.runs == [
        (("load-buffer", "-"), b'a\nb `x` "q" $HOME'),
        (("paste-buffer", "-p", "-d", "-t", "voice:convo"), None),
        (("sleep", "0.5"), None),
        (("send-keys", "-t", "voice:convo", "Enter"), None),
    ]


async def test_type_line_sends_literal_keystrokes():
    tmux = RecordingTmux()
    await tmux.type_line("voice:convo", "/compact")
    assert [run[0] for run in tmux.runs] == [
        ("send-keys", "-t", "voice:convo", "-l", "/compact"),
        ("send-keys", "-t", "voice:convo", "Enter"),
    ]


async def test_ensure_session_creates_when_missing():
    tmux = RecordingTmux(has_session_code=1)
    await tmux.ensure_session("voice")
    assert tmux.runs[0][0] == ("has-session", "-t", "voice")
    new_session = tmux.runs[1][0]
    assert new_session[:8] == ("new-session", "-d", "-s", "voice", "-x", "220", "-y", "50")
    assert new_session[8] == "-c"
    assert tmux.runs[2][0] == (
        "set-environment", "-t", "voice", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "1"
    )


async def test_ensure_session_noop_when_present():
    tmux = RecordingTmux(has_session_code=0)
    await tmux.ensure_session("voice")
    assert [run[0] for run in tmux.runs] == [
        ("has-session", "-t", "voice"),
        ("set-environment", "-t", "voice", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "1"),
    ]
