"""SQLite persistence (stdlib sqlite3, WAL): device credentials, the convo session id,
workstreams, and the plan/inject since-marker. Conversation content is NOT here —
Claude Code's JSONL transcripts are the source of truth.

One connection guarded by a lock — a single process, low write rates.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    webauthn_credential_id TEXT NOT NULL UNIQUE,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS convo (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cc_session_id TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'claude'
);
CREATE TABLE IF NOT EXISTS workstreams (
    name TEXT PRIMARY KEY,
    cc_session_id TEXT NOT NULL,
    window TEXT NOT NULL,
    title TEXT NOT NULL,
    plan_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'fable',
    engine TEXT NOT NULL DEFAULT 'claude'
);
CREATE TABLE IF NOT EXISTS marker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS transcript;
"""


@dataclass
class ConvoRow:
    cc_session_id: str
    engine: str


@dataclass
class WorkstreamRow:
    name: str
    cc_session_id: str
    window: str
    title: str
    plan_path: str
    created_at: float
    status: str
    model: str
    engine: str


@dataclass
class CredentialRow:
    id: str
    name: str
    created_at: float
    revoked_at: float | None


@dataclass
class Passkey:
    id: str
    public_key: bytes
    sign_count: int


class Store:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Auth rework migration: pre-passkey credentials rows have no public key
            # and can't log in — drop the table; devices re-pair (deliberate).
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(credentials)")}
            if columns and "webauthn_credential_id" not in columns:
                self._conn.execute("DROP TABLE credentials")
            self._conn.executescript(_SCHEMA)
            ws_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(workstreams)")}
            if "model" not in ws_columns:  # pre-model-picker rows were all fable
                self._conn.execute(
                    "ALTER TABLE workstreams ADD COLUMN model TEXT NOT NULL DEFAULT 'fable'"
                )
            if "engine" not in ws_columns:  # pre-codex rows were all Claude Code
                self._conn.execute(
                    "ALTER TABLE workstreams ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'"
                )
            convo_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(convo)")}
            if "engine" not in convo_columns:
                self._conn.execute(
                    "ALTER TABLE convo ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'"
                )
            for old, new in (
                ("luna", "gpt-5.6-luna"),
                ("terra", "gpt-5.6-terra"),
                ("sol", "gpt-5.6-sol"),
            ):
                self._conn.execute(
                    "UPDATE settings SET value = ? WHERE value = ?", (new, old)
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- convo (single row: the one persistent conversation session) ----

    def get_convo_session(self) -> ConvoRow | None:
        row = self._fetchone("SELECT cc_session_id, engine FROM convo WHERE id = 1")
        return ConvoRow(**dict(row)) if row else None

    def set_convo_session(self, cc_session_id: str, engine: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO convo (id, cc_session_id, engine) VALUES (1, ?, ?)",
            (cc_session_id, engine),
        )

    # ---- workstreams ----

    def add_workstream(
        self,
        name: str,
        cc_session_id: str,
        window: str,
        title: str,
        plan_path: str,
        model: str,
        engine: str,
    ) -> None:
        self._write(
            "INSERT OR REPLACE INTO workstreams"
            " (name, cc_session_id, window, title, plan_path, created_at, status, model, engine)"
            " VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (name, cc_session_id, window, title, plan_path, time.time(), model, engine),
        )

    def list_workstreams(self) -> list[WorkstreamRow]:
        rows = self._fetchall("SELECT * FROM workstreams ORDER BY created_at")
        return [WorkstreamRow(**dict(row)) for row in rows]

    def set_workstream_status(self, name: str, status: str) -> None:
        self._write("UPDATE workstreams SET status = ? WHERE name = ?", (status, name))

    def remove_workstream(self, name: str) -> None:
        self._write("DELETE FROM workstreams WHERE name = ?", (name,))

    # ---- since-marker (convo transcript line count at the last plan/inject) ----

    def get_marker(self) -> int:
        row = self._fetchone("SELECT last_line FROM marker WHERE id = 1")
        return row["last_line"] if row else 0

    def set_marker(self, last_line: int) -> None:
        self._write("INSERT OR REPLACE INTO marker (id, last_line) VALUES (1, ?)", (last_line,))

    # ---- settings (key/value: convo_model, workstream_model) ----

    def get_setting(self, key: str) -> str | None:
        row = self._fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._write("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # ---- credentials ----

    def create_credential(
        self, name: str, webauthn_credential_id: str, public_key: bytes, sign_count: int
    ) -> str:
        credential_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT OR REPLACE INTO credentials"
            " (id, name, webauthn_credential_id, public_key, sign_count, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (credential_id, name, webauthn_credential_id, public_key, sign_count, time.time()),
        )
        return credential_id

    def get_passkey(self, webauthn_credential_id: str) -> Passkey | None:
        row = self._fetchone(
            "SELECT id, public_key, sign_count FROM credentials"
            " WHERE webauthn_credential_id = ? AND revoked_at IS NULL",
            (webauthn_credential_id,),
        )
        return Passkey(**dict(row)) if row else None

    def set_sign_count(self, credential_id: str, sign_count: int) -> None:
        self._write(
            "UPDATE credentials SET sign_count = ? WHERE id = ?", (sign_count, credential_id)
        )

    def list_credentials(self) -> list[CredentialRow]:
        rows = self._fetchall(
            "SELECT id, name, created_at, revoked_at FROM credentials ORDER BY created_at"
        )
        return [CredentialRow(**dict(row)) for row in rows]

    def revoke_credential(self, credential_id: str) -> bool:
        count = self._write(
            "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), credential_id),
        )
        return count > 0
