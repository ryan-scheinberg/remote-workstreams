import time
from types import SimpleNamespace

import pytest

from voicecode.server import auth
from voicecode.server.auth import (
    LOCKOUT_ATTEMPTS,
    LoginError,
    LoginManager,
    PairingError,
    PairingManager,
    hash_secret,
)
from voicecode.server.store import Store

RP_ID = "mac.tail1234.ts.net"
ORIGIN = f"https://{RP_ID}"


def test_hash_secret_frozen_vector():
    # Golden vector for the cross-unit contract with the deploy plugin.
    assert hash_secret("test-token") == (
        "25826109d479e166c9f242a8fb26638815b009e25bd821b34291272d36e0a5e8"
        "6cb319de3db7099ec66a3dee9eb067ca8c1f9918f7d35d03fbf41209960a7c47"
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.sqlite3")


@pytest.fixture
def pairing_env(monkeypatch):
    # keychain.get_secret reads this env var before touching the Keychain
    monkeypatch.setenv("PIN_HASH", hash_secret("1234"))


def fake_registration(**kwargs):
    return SimpleNamespace(
        credential_id=b"passkey-raw-id", credential_public_key=b"cose-public-key", sign_count=7
    )


def register_device(store, monkeypatch) -> None:
    monkeypatch.setenv("PIN_HASH", hash_secret("1234"))
    monkeypatch.setattr(auth, "verify_registration_response", fake_registration)
    manager = PairingManager(store)
    pairing_id, _ = manager.start("1234", RP_ID)
    manager.finish(pairing_id, {"id": "x"}, ORIGIN)


# ---- pairing ----


def test_start_rejects_bad_pin(store, pairing_env):
    manager = PairingManager(store)
    with pytest.raises(PairingError):
        manager.start("0000", RP_ID)
    pairing_id, _ = manager.start("1234", RP_ID)  # a bad attempt doesn't block the right PIN
    assert pairing_id


def test_start_rejects_when_pairing_not_configured(store, monkeypatch):
    monkeypatch.setattr("voicecode.keychain.get_secret", lambda name: None)
    with pytest.raises(PairingError):
        PairingManager(store).start("1234", RP_ID)


def test_start_locks_out_after_repeated_failures(store, pairing_env):
    manager = PairingManager(store)
    for _ in range(LOCKOUT_ATTEMPTS):
        with pytest.raises(PairingError, match="invalid pin"):
            manager.start("0000", RP_ID)
    # locked: even the right PIN is refused
    with pytest.raises(PairingError, match="too many failed attempts"):
        manager.start("1234", RP_ID)

    manager._locked_until = time.time() - 1  # lockout expired
    pairing_id, _ = manager.start("1234", RP_ID)
    assert pairing_id


def test_success_resets_the_failure_count(store, pairing_env):
    manager = PairingManager(store)
    for _ in range(LOCKOUT_ATTEMPTS - 1):
        with pytest.raises(PairingError):
            manager.start("0000", RP_ID)
    manager.start("1234", RP_ID)
    with pytest.raises(PairingError):  # one more miss must NOT lock (counter was reset)
        manager.start("0000", RP_ID)
    manager.start("1234", RP_ID)


def test_start_returns_creation_options_for_request_host(store, pairing_env):
    manager = PairingManager(store)
    pairing_id, options = manager.start("1234", RP_ID)
    assert pairing_id
    assert options["rp"] == {"id": RP_ID, "name": "voice-code"}
    assert isinstance(options["challenge"], str) and options["challenge"]
    assert options["pubKeyCredParams"]
    assert options["authenticatorSelection"]["authenticatorAttachment"] == "platform"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["authenticatorSelection"]["residentKey"] == "required"


def test_finish_verifies_and_stores_the_passkey(store, pairing_env, monkeypatch):
    calls = {}

    def fake_verify(**kwargs):
        calls.update(kwargs)
        return fake_registration()

    monkeypatch.setattr(auth, "verify_registration_response", fake_verify)
    manager = PairingManager(store)
    pairing_id, _ = manager.start("1234", RP_ID)
    attestation = {"id": "cred", "response": {}}

    credential_id = manager.finish(pairing_id, attestation, ORIGIN)

    assert calls["credential"] == attestation
    assert isinstance(calls["expected_challenge"], bytes)
    assert calls["expected_rp_id"] == RP_ID
    assert calls["expected_origin"] == ORIGIN
    # the passkey is stored, keyed by the base64url webauthn credential id
    passkey = store.get_passkey("cGFzc2tleS1yYXctaWQ")
    assert passkey is not None
    assert passkey.public_key == b"cose-public-key"
    assert passkey.sign_count == 7
    assert store.list_credentials()[0].id == credential_id


def test_finish_rejects_unknown_and_reused_pairing_id(store, pairing_env, monkeypatch):
    monkeypatch.setattr(auth, "verify_registration_response", fake_registration)
    manager = PairingManager(store)
    with pytest.raises(PairingError):
        manager.finish("nope", {}, ORIGIN)

    pairing_id, _ = manager.start("1234", RP_ID)
    manager.finish(pairing_id, {}, ORIGIN)
    with pytest.raises(PairingError):  # single use
        manager.finish(pairing_id, {}, ORIGIN)


def test_finish_rejects_failed_webauthn_verification(store, pairing_env, monkeypatch):
    from webauthn.helpers.exceptions import InvalidRegistrationResponse

    def failing_verify(**kwargs):
        raise InvalidRegistrationResponse("bad attestation")

    monkeypatch.setattr(auth, "verify_registration_response", failing_verify)
    manager = PairingManager(store)
    pairing_id, _ = manager.start("1234", RP_ID)
    with pytest.raises(PairingError):
        manager.finish(pairing_id, {}, ORIGIN)
    assert store.list_credentials() == []


# ---- login ----


def test_login_start_uses_discoverable_credentials(store):
    login_id, options = LoginManager(store).start(RP_ID)
    assert login_id
    assert options["rpId"] == RP_ID
    assert isinstance(options["challenge"], str) and options["challenge"]
    assert options["userVerification"] == "required"
    assert options.get("allowCredentials", []) == []  # the phone offers its own passkey


def test_login_finish_verifies_and_mints_a_session(store, monkeypatch):
    register_device(store, monkeypatch)
    calls = {}

    def fake_verify(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(new_sign_count=8)

    monkeypatch.setattr(auth, "verify_authentication_response", fake_verify)
    manager = LoginManager(store)
    login_id, _ = manager.start(RP_ID)
    assertion = {"id": "cGFzc2tleS1yYXctaWQ", "response": {}}

    token = manager.finish(login_id, assertion, ORIGIN)

    assert calls["credential"] == assertion
    assert isinstance(calls["expected_challenge"], bytes)
    assert calls["expected_rp_id"] == RP_ID
    assert calls["expected_origin"] == ORIGIN
    assert calls["credential_public_key"] == b"cose-public-key"
    assert calls["credential_current_sign_count"] == 7
    assert len(token) >= 32
    assert manager.session_ok(token)
    # the sign count advanced in the store
    assert store.get_passkey("cGFzc2tleS1yYXctaWQ").sign_count == 8


def test_login_finish_rejects_unknown_credential_and_reused_login_id(store, monkeypatch):
    register_device(store, monkeypatch)
    monkeypatch.setattr(
        auth, "verify_authentication_response", lambda **kw: SimpleNamespace(new_sign_count=8)
    )
    manager = LoginManager(store)
    with pytest.raises(LoginError):
        manager.finish("nope", {"id": "cGFzc2tleS1yYXctaWQ"}, ORIGIN)

    login_id, _ = manager.start(RP_ID)
    with pytest.raises(LoginError, match="unknown credential"):
        manager.finish(login_id, {"id": "somebody-else"}, ORIGIN)
    with pytest.raises(LoginError):  # single use, even after a failed lookup
        manager.finish(login_id, {"id": "cGFzc2tleS1yYXctaWQ"}, ORIGIN)


def test_login_finish_rejects_bad_assertion(store, monkeypatch):
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    register_device(store, monkeypatch)

    def failing_verify(**kwargs):
        raise InvalidAuthenticationResponse("bad signature")

    monkeypatch.setattr(auth, "verify_authentication_response", failing_verify)
    manager = LoginManager(store)
    login_id, _ = manager.start(RP_ID)
    with pytest.raises(LoginError):
        manager.finish(login_id, {"id": "cGFzc2tleS1yYXctaWQ"}, ORIGIN)


def test_login_finish_rejects_revoked_passkey(store, monkeypatch):
    register_device(store, monkeypatch)
    store.revoke_credential(store.list_credentials()[0].id)
    manager = LoginManager(store)
    login_id, _ = manager.start(RP_ID)
    with pytest.raises(LoginError, match="unknown credential"):
        manager.finish(login_id, {"id": "cGFzc2tleS1yYXctaWQ"}, ORIGIN)


def test_session_tokens_expire(store):
    manager = LoginManager(store)
    token = manager.mint()
    assert manager.session_ok(token)
    (key,) = manager._sessions
    manager._sessions[key] = time.time() - 1  # 24h later
    assert not manager.session_ok(token)


def test_session_ok_requires_a_live_token(store):
    manager = LoginManager(store)
    assert not manager.session_ok(None)
    assert not manager.session_ok("")
    assert not manager.session_ok("never-minted")
