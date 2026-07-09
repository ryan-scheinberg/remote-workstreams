"""Runtime configuration. Defaults here, overrides via REMOTE_WORKSTREAMS_* env vars.

Secrets are never config — they come from the macOS Keychain (see keychain.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"REMOTE_WORKSTREAMS_{name}", default)


def _default_data_dir() -> Path:
    return Path.home() / ".remote-workstreams"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8400
    data_dir: Path = field(default_factory=_default_data_dir)
    codex_command: str = "codex"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "store.sqlite3"

    @classmethod
    def load(cls) -> "Config":
        return cls(
            host=_env("HOST", cls.host),
            port=int(_env("PORT", str(cls.port))),
            data_dir=Path(_env("DATA_DIR", str(_default_data_dir()))).expanduser(),
            codex_command=_env("CODEX_COMMAND", cls.codex_command),
        )
