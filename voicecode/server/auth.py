"""Auth: scrypt secret hashing, the pairing flow, credential validation.

Pairing (once per device): token + PIN — hashes live in the Keychain, written by
the deploy plugin via this module's hash_secret — then WebAuthn registration
(Face ID), then a long-lived random credential whose scrypt hash is stored.
Reconnects present the credential; no re-auth.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any

from webauthn import generate_registration_options, verify_registration_response
from webauthn.helpers import options_to_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from voicecode import keychain
from voicecode.server.store import Store

PAIRING_TTL_SECONDS = 600


def hash_secret(value: str) -> str:
    """Frozen cross-unit contract — the deploy plugin calls this exact function
    when writing pairing-token-hash / pin-hash to the Keychain. Never change it."""
    return hashlib.scrypt(value.encode(), salt=b"voice-code-v1", n=16384, r=8, p=1).hex()


def _matches(value: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_secret(value), stored_hash)


def verify_pairing_secrets(token: str, pin: str) -> bool:
    token_ok = _matches(token, keychain.get_secret("pairing-token-hash"))
    pin_ok = _matches(pin, keychain.get_secret("pin-hash"))
    return token_ok and pin_ok


def credential_ok(store: Store, credential: str | None) -> bool:
    if not credential:
        return False
    return store.credential_valid(hash_secret(credential))


class PairingError(Exception):
    pass


class PairingManager:
    """In-flight WebAuthn registrations: pairing_id -> (challenge, rp_id, started_at)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._pending: dict[str, tuple[bytes, str, float]] = {}

    def start(self, token: str, pin: str, rp_id: str) -> tuple[str, dict[str, Any]]:
        if not verify_pairing_secrets(token, pin):
            raise PairingError("invalid token or pin")
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="voice-code",
            user_name="voice-code",
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        pairing_id = uuid.uuid4().hex
        self._prune()
        self._pending[pairing_id] = (options.challenge, rp_id, time.time())
        return pairing_id, json.loads(options_to_json(options))

    def finish(self, pairing_id: str, attestation: dict[str, Any], origin: str) -> tuple[str, str]:
        """Verify the attestation; mint and return (credential plaintext, credential_id).

        The plaintext is returned exactly once — only its scrypt hash is stored.
        """
        entry = self._pending.pop(pairing_id, None)
        if entry is None or time.time() - entry[2] > PAIRING_TTL_SECONDS:
            raise PairingError("unknown or expired pairing_id")
        challenge, rp_id, _ = entry
        try:
            verify_registration_response(
                credential=attestation,
                expected_challenge=challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
            )
        except WebAuthnException as exc:
            raise PairingError(f"registration verification failed: {exc}") from exc
        credential = secrets.token_urlsafe(32)
        name = f"device-{time.strftime('%Y-%m-%d')}"
        credential_id = self.store.create_credential(name, hash_secret(credential))
        return credential, credential_id

    def _prune(self) -> None:
        cutoff = time.time() - PAIRING_TTL_SECONDS
        self._pending = {k: v for k, v in self._pending.items() if v[2] >= cutoff}
