from pathlib import Path

import pytest

from server_fakes import FakeSubstrate
from voicecode.bootstrap import ensure_convo
from voicecode.server.store import Store

PLUGIN_DIR = Path("/plugins/claude-code")


@pytest.fixture
def rig(tmp_path):
    return Store(tmp_path / "db.sqlite3"), FakeSubstrate(tmp_path / "transcripts")


async def test_fresh_spawn_mints_id_and_stores_it(rig):
    store, substrate = rig
    session = await ensure_convo(store, substrate, PLUGIN_DIR)
    (spawned,) = substrate.spawned
    assert spawned is session
    spec = session.spec
    # CONVO_MODEL is sonnet for now (token thrift); design intent is fable low.
    assert (spec.name, spec.model, spec.effort) == ("convo", "sonnet", "low")
    assert spec.display_name == "convo"
    assert spec.plugin_dir == PLUGIN_DIR
    assert spec.initial_prompt == "/voice-code:role-convo"
    assert spec.resume is False
    assert store.get_convo_session() == session.session_id
    assert session.window == "voice:convo"


async def test_alive_window_is_reused_without_spawning(rig):
    store, substrate = rig
    store.set_convo_session("cc-stored")
    substrate.alive_windows.add("voice:convo")
    session = await ensure_convo(store, substrate, PLUGIN_DIR)
    assert substrate.spawned == []
    assert session.session_id == "cc-stored"
    assert session.window == "voice:convo"
    assert session.transcript == substrate.transcript_dir / "cc-stored.jsonl"
    assert session.spec.plugin_dir == PLUGIN_DIR


async def test_dead_window_respawns_with_resume(rig):
    store, substrate = rig
    store.set_convo_session("cc-stored")  # remembered, but no window alive
    session = await ensure_convo(store, substrate, PLUGIN_DIR)
    (spawned,) = substrate.spawned
    assert spawned is session
    assert session.session_id == "cc-stored"  # continuity across reboots
    assert session.spec.resume is True
    assert session.spec.initial_prompt is None  # the role is already in its history
    assert store.get_convo_session() == "cc-stored"
