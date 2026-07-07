"""How the persistent convo session comes to exist — the one place that knows.

The stored CC session id survives reboots: alive tmux window → reuse it; dead
window → respawn with --resume for full conversational continuity; nothing
stored → fresh spawn with the role-convo skill.
"""

from __future__ import annotations

from pathlib import Path

from remote_workstreams.server.store import Store
from remote_workstreams.substrate import CCSession, SessionSpec, Substrate

CONVO_MODEL = "fable"
CONVO_EFFORT = "low"
CONVO_WINDOW = "convo"


def _spec(
    plugin_dir: Path, *, model: str = CONVO_MODEL, initial_prompt: str | None = None,
    resume: bool = False,
):
    return SessionSpec(
        name=CONVO_WINDOW,
        model=model,
        effort=CONVO_EFFORT,
        display_name=CONVO_WINDOW,
        plugin_dir=plugin_dir,
        initial_prompt=initial_prompt,
        resume=resume,
        remote_control=True,  # convo shows up in the iOS Claude app too
    )


async def ensure_convo(store: Store, substrate: Substrate, plugin_dir: Path) -> CCSession:
    stored = store.get_convo_session()
    if stored is None:
        return await _spawn_fresh(store, substrate, plugin_dir)
    existing = CCSession(
        session_id=stored,
        window=f"voice:{CONVO_WINDOW}",  # the tmux session name is pinned system-wide
        transcript=substrate.transcript_dir / f"{stored}.jsonl",
        spec=_spec(plugin_dir),
    )
    if await substrate.alive(existing):
        return existing
    model = store.get_setting("convo_model") or CONVO_MODEL
    return await substrate.spawn(_spec(plugin_dir, model=model, resume=True), session_id=stored)


async def fresh_convo(store: Store, substrate: Substrate, plugin_dir: Path) -> CCSession:
    """The Clear button: kill the current convo session and start over clean."""
    stored = store.get_convo_session()
    if stored is not None:
        old = CCSession(
            session_id=stored,
            window=f"voice:{CONVO_WINDOW}",
            transcript=substrate.transcript_dir / f"{stored}.jsonl",
            spec=_spec(plugin_dir),
        )
        if await substrate.alive(old):
            await substrate.kill(old)
    session = await _spawn_fresh(store, substrate, plugin_dir)
    store.set_marker(0)  # the marker counted lines of a transcript that's now history
    return session


async def _spawn_fresh(store: Store, substrate: Substrate, plugin_dir: Path) -> CCSession:
    model = store.get_setting("convo_model") or CONVO_MODEL
    session = await substrate.spawn(
        _spec(plugin_dir, model=model, initial_prompt="/remote-workstreams:role-convo")
    )
    store.set_convo_session(session.session_id)
    return session
