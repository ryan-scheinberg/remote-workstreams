import pytest
from starlette.testclient import TestClient

from server_fakes import Fakes, make_app
from voicecode.server import auth
from voicecode.server.auth import hash_secret

AUTH = {"Authorization": "Bearer cred-1"}


@pytest.fixture
def client(tmp_path):
    app = make_app(tmp_path, Fakes())
    with TestClient(app) as client:
        client.app_state = app.state
        yield client


def seed_credential(client, plaintext="cred-1"):
    return client.app_state.store.create_credential("test-device", hash_secret(plaintext))


def test_healthz_no_auth(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_pairing_rest_flow(client, monkeypatch):
    monkeypatch.setenv("PAIRING_TOKEN_HASH", hash_secret("test-token"))
    monkeypatch.setenv("PIN_HASH", hash_secret("1234"))
    monkeypatch.setattr(auth, "verify_registration_response", lambda **kwargs: None)

    assert client.post("/api/pair/start", json={"token": "bad", "pin": "1234"}).status_code == 403

    start = client.post("/api/pair/start", json={"token": "test-token", "pin": "1234"})
    assert start.status_code == 200
    body = start.json()
    assert body["registration_options"]["rp"]["id"] == "testserver"  # RP ID from request host

    finish = client.post(
        "/api/pair/finish",
        json={"pairing_id": body["pairing_id"], "attestation": {"id": "x"}},
        headers={"Origin": "https://testserver"},
    )
    assert finish.status_code == 200
    credential = finish.json()["credential"]
    credential_id = finish.json()["credential_id"]

    # the minted credential works as a Bearer token
    headers = {"Authorization": f"Bearer {credential}"}
    assert client.get("/api/sessions", headers=headers).status_code == 200

    revoke = client.post(f"/api/credentials/{credential_id}/revoke", headers=headers)
    assert revoke.status_code == 200 and revoke.json() == {"ok": True}
    assert client.get("/api/sessions", headers=headers).status_code == 401


def test_pair_finish_unknown_pairing_id(client):
    response = client.post("/api/pair/finish", json={"pairing_id": "nope", "attestation": {}})
    assert response.status_code == 403


def test_sessions_requires_bearer(client):
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401

    seed_credential(client)
    client.app_state.store.create_session("Fix the tests")
    response = client.get("/api/sessions", headers=AUTH)
    assert response.status_code == 200
    sessions = response.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Fix the tests"
    assert set(sessions[0]) == {"id", "title", "created_at", "last_active"}


def test_credentials_listing_and_revoke(client):
    cred_id = seed_credential(client)
    response = client.get("/api/credentials", headers=AUTH)
    assert response.status_code == 200
    creds = response.json()["credentials"]
    assert creds[0]["id"] == cred_id and creds[0]["name"] == "test-device"
    assert "secret_hash" not in creds[0]

    assert client.post("/api/credentials/missing/revoke", headers=AUTH).status_code == 404
    assert client.post(f"/api/credentials/{cred_id}/revoke", headers=AUTH).status_code == 200
    # the revoked credential can no longer call the API
    assert client.get("/api/credentials", headers=AUTH).status_code == 401


def test_static_pwa_with_spa_fallback(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>PWA</html>")
    (web / "app.js").write_text("console.log('hi')")
    app = make_app(tmp_path, Fakes(), web_dir=web)
    with TestClient(app) as client:
        assert client.get("/").text == "<html>PWA</html>"
        assert client.get("/app.js").text == "console.log('hi')"
        assert client.get("/some/client/route").text == "<html>PWA</html>"  # SPA fallback
        assert client.get("/api/unknown").status_code == 404  # API paths never fall back


def test_static_missing_web_dir_is_a_404_not_a_crash(tmp_path):
    app = make_app(tmp_path, Fakes(), web_dir=tmp_path / "nonexistent")
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
