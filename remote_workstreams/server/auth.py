"""Auth: scrypt secret hashing, the pairing flow, the login flow.

Pairing (once per device): PIN — its hash lives in the Keychain, written by the
deploy plugin via this module's hash_secret — then WebAuthn registration
(Face ID); the passkey's public key is stored. Login (every app open): a
WebAuthn assertion against a stored passkey mints a session token held only in
memory with a 24h TTL — a server restart logs every device out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, options_to_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from remote_workstreams import keychain
from remote_workstreams.server.store import Store

PAIRING_TTL_SECONDS = 600
LOCKOUT_ATTEMPTS = 5
LOCKOUT_SECONDS = 600
LOGIN_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 24 * 3600

logger = logging.getLogger("remote_workstreams.server.auth")


def hash_secret(value: str) -> str:
    """Frozen cross-unit contract — the deploy plugin calls this exact function
    when writing pin-hash to the Keychain. Never change it."""
    return hashlib.scrypt(value.encode(), salt=b"voice-code-v1", n=16384, r=8, p=1).hex()


def verify_pairing_secrets(pin: str) -> bool:
    stored = keychain.get_secret("pin-hash")
    if not stored:
        return False
    return hmac.compare_digest(hash_secret(pin), stored)


class PairingError(Exception):
    pass


class LoginError(Exception):
    pass


class PairingManager:
    """In-flight WebAuthn registrations: pairing_id -> (challenge, rp_id, started_at)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._pending: dict[str, tuple[bytes, str, float]] = {}
        self._failures = 0
        self._locked_until = 0.0

    def start(self, pin: str, rp_id: str) -> tuple[str, dict[str, Any]]:
        if time.time() < self._locked_until:
            raise PairingError("too many failed attempts; try again later")
        if not verify_pairing_secrets(pin):
            self._failures += 1
            if self._failures >= LOCKOUT_ATTEMPTS:
                self._locked_until = time.time() + LOCKOUT_SECONDS
                self._failures = 0
                logger.warning(
                    "pairing_locked_out", extra={"fields": {"lockout_seconds": LOCKOUT_SECONDS}}
                )
            raise PairingError("invalid pin")
        self._failures = 0
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="remote-workstreams",
            user_name="remote-workstreams",
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        pairing_id = uuid.uuid4().hex
        self._prune()
        self._pending[pairing_id] = (options.challenge, rp_id, time.time())
        return pairing_id, json.loads(options_to_json(options))

    def finish(self, pairing_id: str, attestation: dict[str, Any], origin: str) -> str:
        """Verify the attestation, store the passkey; return the credential row id."""
        entry = self._pending.pop(pairing_id, None)
        if entry is None or time.time() - entry[2] > PAIRING_TTL_SECONDS:
            raise PairingError("unknown or expired pairing_id")
        challenge, rp_id, _ = entry
        try:
            verified = verify_registration_response(
                credential=attestation,
                expected_challenge=challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
            )
        except WebAuthnException as exc:
            raise PairingError(f"registration verification failed: {exc}") from exc
        name = f"device-{time.strftime('%Y-%m-%d')}"
        return self.store.create_credential(
            name,
            bytes_to_base64url(verified.credential_id),
            verified.credential_public_key,
            verified.sign_count,
        )

    def _prune(self) -> None:
        cutoff = time.time() - PAIRING_TTL_SECONDS
        self._pending = {k: v for k, v in self._pending.items() if v[2] >= cutoff}


def _session_hash(token: str) -> str:
    # Session tokens are 256-bit random; a fast hash only keeps plaintext out of memory dumps.
    return hashlib.sha256(token.encode()).hexdigest()


class LoginManager:
    """In-flight WebAuthn assertions (login_id -> challenge) and live session
    tokens (hash -> expiry). Both in-memory only."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._pending: dict[str, tuple[bytes, str, float]] = {}
        self._sessions: dict[str, float] = {}

    def start(self, rp_id: str) -> tuple[str, dict[str, Any]]:
        # Empty allow_credentials: the phone offers its discoverable passkey itself.
        options = generate_authentication_options(
            rp_id=rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        login_id = uuid.uuid4().hex
        self._prune()
        self._pending[login_id] = (options.challenge, rp_id, time.time())
        return login_id, json.loads(options_to_json(options))

    def finish(self, login_id: str, assertion: dict[str, Any], origin: str) -> str:
        """Verify the assertion against the stored passkey; mint a session token."""
        entry = self._pending.pop(login_id, None)
        if entry is None or time.time() - entry[2] > LOGIN_TTL_SECONDS:
            raise LoginError("unknown or expired login_id")
        challenge, rp_id, _ = entry
        passkey = self.store.get_passkey(str(assertion.get("id", "")))
        if passkey is None:
            raise LoginError("unknown credential")
        try:
            verified = verify_authentication_response(
                credential=assertion,
                expected_challenge=challenge,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            raise LoginError(f"authentication verification failed: {exc}") from exc
        self.store.set_sign_count(passkey.id, verified.new_sign_count)
        return self.mint()

    def mint(self) -> str:
        token = secrets.token_urlsafe(32)
        self._prune_sessions()
        self._sessions[_session_hash(token)] = time.time() + SESSION_TTL_SECONDS
        return token

    def session_ok(self, token: str | None) -> bool:
        if not token:
            return False
        expiry = self._sessions.get(_session_hash(token))
        return expiry is not None and expiry > time.time()

    def _prune(self) -> None:
        cutoff = time.time() - LOGIN_TTL_SECONDS
        self._pending = {k: v for k, v in self._pending.items() if v[2] >= cutoff}

    def _prune_sessions(self) -> None:
        now = time.time()
        self._sessions = {k: v for k, v in self._sessions.items() if v > now}
