"""SQLite persistence (stdlib sqlite3, WAL): sessions, credentials, transcript log.

One connection guarded by a lock — a single process, one row per committed turn.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voicecode.protocol import SessionInfo

DEFAULT_TITLE = "New session"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_active REAL NOT NULL,
    messages TEXT NOT NULL DEFAULT '[]',
    execution_session_id TEXT
);
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS transcript (
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript (session_id, ts);
"""


@dataclass
class SessionRow:
    id: str
    title: str
    created_at: float
    last_active: float
    messages: list[dict[str, Any]]
    execution_session_id: str | None


@dataclass
class CredentialRow:
    id: str
    name: str
    created_at: float
    revoked_at: float | None


class Store:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
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

    # ---- sessions ----

    def create_session(self, title: str = DEFAULT_TITLE) -> SessionRow:
        now = time.time()
        session_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO sessions (id, title, created_at, last_active) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now),
        )
        return SessionRow(session_id, title, now, now, [], None)

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRow:
        return SessionRow(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            last_active=row["last_active"],
            messages=json.loads(row["messages"]),
            execution_session_id=row["execution_session_id"],
        )

    def get_session(self, session_id: str) -> SessionRow | None:
        row = self._fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._session(row) if row else None

    def most_recent_session(self) -> SessionRow | None:
        row = self._fetchone("SELECT * FROM sessions ORDER BY last_active DESC LIMIT 1")
        return self._session(row) if row else None

    def list_sessions(self) -> list[SessionInfo]:
        rows = self._fetchall(
            "SELECT id, title, created_at, last_active FROM sessions ORDER BY last_active DESC"
        )
        return [SessionInfo(**dict(row)) for row in rows]

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._write(
            "UPDATE sessions SET messages = ?, last_active = ? WHERE id = ?",
            (json.dumps(messages), time.time(), session_id),
        )

    def set_execution_session(self, session_id: str, execution_session_id: str) -> None:
        self._write(
            "UPDATE sessions SET execution_session_id = ? WHERE id = ?",
            (execution_session_id, session_id),
        )

    def set_title_if_default(self, session_id: str, title: str) -> None:
        self._write(
            "UPDATE sessions SET title = ? WHERE id = ? AND title = ?",
            (title, session_id, DEFAULT_TITLE),
        )

    def touch(self, session_id: str) -> None:
        self._write(
            "UPDATE sessions SET last_active = ? WHERE id = ?", (time.time(), session_id)
        )

    # ---- transcript log ----

    def add_transcript(self, session_id: str, role: str, text: str) -> None:
        self._write(
            "INSERT INTO transcript (session_id, ts, role, text) VALUES (?, ?, ?, ?)",
            (session_id, time.time(), role, text),
        )

    def get_transcript(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT ts, role, text FROM transcript WHERE session_id = ? ORDER BY ts",
            (session_id,),
        )
        return [dict(row) for row in rows]

    # ---- credentials ----

    def create_credential(self, name: str, secret_hash: str) -> str:
        credential_id = uuid.uuid4().hex[:12]
        self._write(
            "INSERT INTO credentials (id, name, secret_hash, created_at) VALUES (?, ?, ?, ?)",
            (credential_id, name, secret_hash, time.time()),
        )
        return credential_id

    def credential_valid(self, secret_hash: str) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM credentials WHERE secret_hash = ? AND revoked_at IS NULL",
            (secret_hash,),
        )
        return row is not None

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
