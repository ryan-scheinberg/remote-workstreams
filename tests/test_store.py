from voicecode.server.auth import hash_secret
from voicecode.server.store import Store


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "data" / "test.sqlite3")


def test_hash_secret_frozen_vector():
    # Golden vector for the cross-unit scrypt contract with the deploy plugin.
    assert hash_secret("test-token") == (
        "25826109d479e166c9f242a8fb26638815b009e25bd821b34291272d36e0a5e8"
        "6cb319de3db7099ec66a3dee9eb067ca8c1f9918f7d35d03fbf41209960a7c47"
    )


def test_convo_session_single_row(tmp_path):
    store = make_store(tmp_path)
    assert store.get_convo_session() is None
    store.set_convo_session("cc-1")
    assert store.get_convo_session() == "cc-1"
    store.set_convo_session("cc-2")  # replace, never a second row
    assert store.get_convo_session() == "cc-2"


def test_workstreams_crud(tmp_path):
    store = make_store(tmp_path)
    assert store.list_workstreams() == []
    store.add_workstream("ws-auth", "cc-1", "voice:ws-auth", "Wire auth", "/plans/plan-1.md")
    store.add_workstream("ws-docs", "cc-2", "voice:ws-docs", "Write docs", "/plans/plan-2.md")
    rows = store.list_workstreams()
    assert [r.name for r in rows] == ["ws-auth", "ws-docs"]
    assert rows[0].cc_session_id == "cc-1"
    assert rows[0].window == "voice:ws-auth"
    assert rows[0].title == "Wire auth"
    assert rows[0].plan_path == "/plans/plan-1.md"
    assert rows[0].status == "running"

    store.set_workstream_status("ws-auth", "gone")
    assert [r.status for r in store.list_workstreams()] == ["gone", "running"]


def test_workstream_same_name_replaces(tmp_path):
    store = make_store(tmp_path)
    store.add_workstream("ws-auth", "cc-1", "voice:ws-auth", "Wire auth", "/p1.md")
    store.add_workstream("ws-auth", "cc-9", "voice:ws-auth", "Wire auth again", "/p2.md")
    rows = store.list_workstreams()
    assert len(rows) == 1
    assert rows[0].cc_session_id == "cc-9"


def test_marker_defaults_to_zero_and_advances(tmp_path):
    store = make_store(tmp_path)
    assert store.get_marker() == 0
    store.set_marker(42)
    assert store.get_marker() == 42
    store.set_marker(99)
    assert store.get_marker() == 99


def test_credentials_lifecycle(tmp_path):
    store = make_store(tmp_path)
    cred_id = store.create_credential("phone", "wcid-1", b"public-key", 3)
    passkey = store.get_passkey("wcid-1")
    assert passkey.id == cred_id and passkey.public_key == b"public-key"
    assert passkey.sign_count == 3
    assert store.get_passkey("wcid-2") is None
    creds = store.list_credentials()
    assert creds[0].id == cred_id and creds[0].name == "phone" and creds[0].revoked_at is None

    store.set_sign_count(cred_id, 4)
    assert store.get_passkey("wcid-1").sign_count == 4

    assert store.revoke_credential(cred_id)
    assert store.get_passkey("wcid-1") is None
    assert not store.revoke_credential(cred_id)  # already revoked
    assert not store.revoke_credential("missing")


def test_repairing_the_same_passkey_replaces_the_row(tmp_path):
    store = make_store(tmp_path)
    store.create_credential("phone", "wcid-1", b"pk-old", 3)
    new_id = store.create_credential("phone", "wcid-1", b"pk-new", 0)
    (row,) = store.list_credentials()
    assert row.id == new_id
    assert store.get_passkey("wcid-1").public_key == b"pk-new"


def test_pre_passkey_credentials_table_is_dropped(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE credentials (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " secret_hash TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL)"
    )
    conn.execute("INSERT INTO credentials VALUES ('c1', 'old-phone', 'h', 1.0, NULL)")
    conn.commit()
    conn.close()

    store = Store(path)  # migration drops the old table; old pairings are gone
    assert store.list_credentials() == []
    store.create_credential("phone", "wcid-1", b"pk", 0)
    assert store.get_passkey("wcid-1") is not None


def test_v4_tables_are_dropped(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = Store(path)
    store.close()
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    store = Store(path)  # re-open runs the schema, which drops v4 leftovers
    names = {
        row[0]
        for row in store._fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "sessions" not in names and "transcript" not in names
    assert {"credentials", "convo", "workstreams", "marker"} <= names


def test_wal_mode(tmp_path):
    store = make_store(tmp_path)
    mode = store._fetchone("PRAGMA journal_mode")[0]
    assert mode == "wal"
