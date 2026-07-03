"""Runtime configuration. Defaults here, overrides via VOICECODE_* env vars.

Secrets are never config — they come from the macOS Keychain (see keychain.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"VOICECODE_{name}", default)


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8400
    conversation_model: str = "claude-haiku-4-5"
    execution_cwd: Path = field(default_factory=Path.home)
    data_dir: Path = field(default_factory=lambda: Path.home() / ".voicecode")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "voicecode.sqlite3"

    @classmethod
    def load(cls) -> "Config":
        return cls(
            host=_env("HOST", cls.host),
            port=int(_env("PORT", str(cls.port))),
            conversation_model=_env("CONVERSATION_MODEL", cls.conversation_model),
            execution_cwd=Path(_env("EXECUTION_CWD", str(Path.home()))).expanduser(),
            data_dir=Path(_env("DATA_DIR", str(Path.home() / ".voicecode"))).expanduser(),
        )
