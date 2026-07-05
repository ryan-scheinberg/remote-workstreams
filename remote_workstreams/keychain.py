"""Secrets via the macOS Keychain (`security` CLI), service name "remote-workstreams".

Env vars win when set (dev and tests); the deploy plugin writes the Keychain entries.
Secret names: deepgram-api-key, cartesia-api-key, pin-hash.
"""

from __future__ import annotations

import subprocess

SERVICE = "remote-workstreams"


def _env_name(name: str) -> str:
    return name.upper().replace("-", "_")


def get_secret(name: str) -> str | None:
    import os

    if value := os.environ.get(_env_name(name)):
        return value
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def set_secret(name: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
        check=True,
        capture_output=True,
    )
