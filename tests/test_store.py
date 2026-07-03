from voicecode.server.store import DEFAULT_TITLE, Store


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "data" / "test.sqlite3")


def test_create_and_get_session(tmp_path):
    store = make_store(tmp_path)
    row = store.create_session()
    got = store.get_session(row.id)
    assert got is not None
    assert got.title == DEFAULT_TITLE
    assert got.messages == []
    assert got.execution_session_id is None
    assert store.get_session("missing") is None


def test_messages_roundtrip_and_execution_session(tmp_path):
    store = make_store(tmp_path)
    row = store.create_session()
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    store.save_messages(row.id, messages)
    store.set_execution_session(row.id, "exec-42")
    got = store.get_session(row.id)
    assert got.messages == messages
    assert got.execution_session_id == "exec-42"


def test_most_recent_session_follows_last_active(tmp_path):
    store = make_store(tmp_path)
    assert store.most_recent_session() is None
    first = store.create_session()
    store.create_session()
    store.touch(first.id)
    assert store.most_recent_session().id == first.id


def test_list_sessions_shape(tmp_path):
    store = make_store(tmp_path)
    row = store.create_session()
    sessions = store.list_sessions()
    assert [s.id for s in sessions] == [row.id]
    assert sessions[0].title == DEFAULT_TITLE


def test_title_set_once_from_first_utterance(tmp_path):
    store = make_store(tmp_path)
    row = store.create_session()
    store.set_title_if_default(row.id, "Fix the retry loop")
    store.set_title_if_default(row.id, "Something else")
    assert store.get_session(row.id).title == "Fix the retry loop"


def test_transcript_log(tmp_path):
    store = make_store(tmp_path)
    row = store.create_session()
    store.add_transcript(row.id, "user", "hello")
    store.add_transcript(row.id, "assistant", "hi there")
    log = store.get_transcript(row.id)
    assert [(e["role"], e["text"]) for e in log] == [("user", "hello"), ("assistant", "hi there")]


def test_credentials_lifecycle(tmp_path):
    store = make_store(tmp_path)
    cred_id = store.create_credential("phone", "hash-1")
    assert store.credential_valid("hash-1")
    assert not store.credential_valid("hash-2")
    creds = store.list_credentials()
    assert creds[0].id == cred_id and creds[0].name == "phone" and creds[0].revoked_at is None

    assert store.revoke_credential(cred_id)
    assert not store.credential_valid("hash-1")
    assert not store.revoke_credential(cred_id)  # already revoked
    assert not store.revoke_credential("missing")


def test_wal_mode(tmp_path):
    store = make_store(tmp_path)
    mode = store._fetchone("PRAGMA journal_mode")[0]
    assert mode == "wal"
