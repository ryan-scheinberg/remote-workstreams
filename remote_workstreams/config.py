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
    stt_provider: str = "deepgram"
    tts_provider: str = "cartesia"
    moonshine_language: str = "en"
    moonshine_stt_model: str = "medium-streaming"
    moonshine_tts_locale: str = "en-us"
    moonshine_tts_voice: str = "kokoro_af_heart"
    moonshine_tts_speed: float = 1.0
    moonshine_model_dir: Path = field(
        default_factory=lambda: _default_data_dir() / "models" / "moonshine"
    )

    def __post_init__(self) -> None:
        allowed_by_kind = {
            "stt_provider": {"deepgram", "moonshine"},
            "tts_provider": {"cartesia", "moonshine"},
        }
        for name, value in (
            ("stt_provider", self.stt_provider),
            ("tts_provider", self.tts_provider),
        ):
            normalized = value.strip().lower()
            allowed = allowed_by_kind[name]
            if normalized not in allowed:
                raise ValueError(
                    f"unsupported {name} {value!r}; use {', '.join(sorted(allowed))}"
                )
            setattr(self, name, normalized)
        if self.moonshine_tts_speed <= 0:
            raise ValueError("moonshine_tts_speed must be greater than zero")

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
            stt_provider=_env("STT_PROVIDER", cls.stt_provider).lower(),
            tts_provider=_env("TTS_PROVIDER", cls.tts_provider).lower(),
            moonshine_language=_env("MOONSHINE_LANGUAGE", cls.moonshine_language),
            moonshine_stt_model=_env("MOONSHINE_STT_MODEL", cls.moonshine_stt_model),
            moonshine_tts_locale=_env("MOONSHINE_TTS_LOCALE", cls.moonshine_tts_locale),
            moonshine_tts_voice=_env("MOONSHINE_TTS_VOICE", cls.moonshine_tts_voice),
            moonshine_tts_speed=float(
                _env("MOONSHINE_TTS_SPEED", str(cls.moonshine_tts_speed))
            ),
            moonshine_model_dir=Path(
                _env(
                    "MOONSHINE_MODEL_DIR",
                    str(_default_data_dir() / "models" / "moonshine"),
                )
            ).expanduser(),
        )
