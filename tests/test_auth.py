import pytest

from voicecode.server import auth
from voicecode.server.auth import PairingError, PairingManager, credential_ok, hash_secret
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
    # keychain.get_secret reads these env vars before touching the Keychain
    monkeypatch.setenv("PAIRING_TOKEN_HASH", hash_secret("test-token"))
    monkeypatch.setenv("PIN_HASH", hash_secret("1234"))


def test_start_rejects_bad_token_and_pin(store, pairing_env):
    manager = PairingManager(store)
    with pytest.raises(PairingError):
        manager.start("wrong-token", "1234", RP_ID)
    with pytest.raises(PairingError):
        manager.start("test-token", "0000", RP_ID)


def test_start_rejects_when_pairing_not_configured(store, monkeypatch):
    monkeypatch.setattr("voicecode.keychain.get_secret", lambda name: None)
    with pytest.raises(PairingError):
        PairingManager(store).start("test-token", "1234", RP_ID)


def test_start_returns_creation_options_for_request_host(store, pairing_env):
    manager = PairingManager(store)
    pairing_id, options = manager.start("test-token", "1234", RP_ID)
    assert pairing_id
    assert options["rp"] == {"id": RP_ID, "name": "voice-code"}
    assert isinstance(options["challenge"], str) and options["challenge"]
    assert options["pubKeyCredParams"]
    assert options["authenticatorSelection"]["authenticatorAttachment"] == "platform"
    assert options["authenticatorSelection"]["userVerification"] == "required"


def test_finish_verifies_and_mints_credential(store, pairing_env, monkeypatch):
    calls = {}

    def fake_verify(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(auth, "verify_registration_response", fake_verify)
    manager = PairingManager(store)
    pairing_id, _ = manager.start("test-token", "1234", RP_ID)
    attestation = {"id": "cred", "response": {}}

    credential, credential_id = manager.finish(pairing_id, attestation, ORIGIN)

    assert calls["credential"] == attestation
    assert isinstance(calls["expected_challenge"], bytes)
    assert calls["expected_rp_id"] == RP_ID
    assert calls["expected_origin"] == ORIGIN
    # long-lived random credential; only the scrypt hash is stored
    assert len(credential) >= 32
    assert credential_ok(store, credential)
    assert store.list_credentials()[0].id == credential_id

    store.revoke_credential(credential_id)
    assert not credential_ok(store, credential)


def test_finish_rejects_unknown_and_reused_pairing_id(store, pairing_env, monkeypatch):
    monkeypatch.setattr(auth, "verify_registration_response", lambda **kwargs: None)
    manager = PairingManager(store)
    with pytest.raises(PairingError):
        manager.finish("nope", {}, ORIGIN)

    pairing_id, _ = manager.start("test-token", "1234", RP_ID)
    manager.finish(pairing_id, {}, ORIGIN)
    with pytest.raises(PairingError):  # single use
        manager.finish(pairing_id, {}, ORIGIN)


def test_finish_rejects_failed_webauthn_verification(store, pairing_env, monkeypatch):
    from webauthn.helpers.exceptions import InvalidRegistrationResponse

    def failing_verify(**kwargs):
        raise InvalidRegistrationResponse("bad attestation")

    monkeypatch.setattr(auth, "verify_registration_response", failing_verify)
    manager = PairingManager(store)
    pairing_id, _ = manager.start("test-token", "1234", RP_ID)
    with pytest.raises(PairingError):
        manager.finish(pairing_id, {}, ORIGIN)
    assert store.list_credentials() == []


def test_credential_ok_requires_value(store):
    assert not credential_ok(store, None)
    assert not credential_ok(store, "")
