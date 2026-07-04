"""How the persistent convo session comes to exist — the one place that knows.

The stored CC session id survives reboots: alive tmux window → reuse it; dead
window → respawn with --resume for full conversational continuity; nothing
stored → fresh spawn with the role-convo skill.
"""

from __future__ import annotations

from pathlib import Path

from voicecode.server.store import Store
from voicecode.substrate import CCSession, SessionSpec, Substrate

CONVO_MODEL = "fable"
CONVO_EFFORT = "low"
CONVO_WINDOW = "convo"


def _spec(plugin_dir: Path, *, initial_prompt: str | None = None, resume: bool = False):
    return SessionSpec(
        name=CONVO_WINDOW,
        model=CONVO_MODEL,
        effort=CONVO_EFFORT,
        display_name=CONVO_WINDOW,
        plugin_dir=plugin_dir,
        initial_prompt=initial_prompt,
        resume=resume,
    )


async def ensure_convo(store: Store, substrate: Substrate, plugin_dir: Path) -> CCSession:
    stored = store.get_convo_session()
    if stored is None:
        session = await substrate.spawn(_spec(plugin_dir, initial_prompt="/voice-code:role-convo"))
        store.set_convo_session(session.session_id)
        return session
    existing = CCSession(
        session_id=stored,
        window=f"voice:{CONVO_WINDOW}",  # the tmux session name is pinned system-wide
        transcript=substrate.transcript_dir / f"{stored}.jsonl",
        spec=_spec(plugin_dir),
    )
    if await substrate.alive(existing):
        return existing
    return await substrate.spawn(_spec(plugin_dir, resume=True), session_id=stored)
