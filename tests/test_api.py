import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from server_fakes import FakeConn, Fakes, make_app
from voicecode.server import auth
from voicecode.server.auth import hash_secret

AUTH = {"Authorization": "Bearer cred-1"}
APPROVAL_HEADERS = {"X-Voicecode-Token": "boot-token"}
BASH_PAYLOAD = {
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /tmp/x"},
}


@pytest.fixture
def client(tmp_path):
    app = make_app(tmp_path, Fakes(tmp_path))
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
    assert client.get("/api/credentials", headers=headers).status_code == 200

    revoke = client.post(f"/api/credentials/{credential_id}/revoke", headers=headers)
    assert revoke.status_code == 200 and revoke.json() == {"ok": True}
    assert client.get("/api/credentials", headers=headers).status_code == 401


def test_pair_finish_unknown_pairing_id(client):
    response = client.post("/api/pair/finish", json={"pairing_id": "nope", "attestation": {}})
    assert response.status_code == 403


def test_credentials_listing_and_revoke(client):
    assert client.get("/api/credentials").status_code == 401
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


# ---- POST /approvals (the phone-approval relay endpoint) ----


def make_async_client(tmp_path):
    app = make_app(tmp_path, Fakes(tmp_path))
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


async def resolve_when_pending(app, approved: bool):
    approvals = app.state.approvals
    while not approvals.pending:
        await asyncio.sleep(0.005)
    (approval_id,) = approvals.pending
    approvals.resolve(approval_id, approved)


async def test_approvals_allow(tmp_path):
    app, client = make_async_client(tmp_path)
    conn = FakeConn()
    app.state.runtime.push.conn = conn  # a phone is "connected"
    async with client:
        task = asyncio.create_task(
            client.post("/approvals", json=BASH_PAYLOAD, headers=APPROVAL_HEADERS)
        )
        await resolve_when_pending(app, approved=True)
        response = await task
    assert response.status_code == 200
    assert response.json() == {"decision": "allow"}
    # the pushed card summarized the Bash call as its command string
    (request,) = conn.messages
    assert request.type == "approval_request"
    assert request.summary == "rm -rf /tmp/x"
    assert request.session == "s1" and request.tool == "Bash"


async def test_approvals_deny(tmp_path):
    app, client = make_async_client(tmp_path)
    async with client:
        task = asyncio.create_task(
            client.post("/approvals", json=BASH_PAYLOAD, headers=APPROVAL_HEADERS)
        )
        await resolve_when_pending(app, approved=False)
        response = await task
    assert response.json() == {"decision": "deny"}


async def test_approvals_non_bash_summary(tmp_path):
    app, client = make_async_client(tmp_path)
    conn = FakeConn()
    app.state.runtime.push.conn = conn
    payload = {"session_id": "s2", "tool_name": "Write", "tool_input": {"file_path": "/tmp/a"}}
    async with client:
        task = asyncio.create_task(
            client.post("/approvals", json=payload, headers=APPROVAL_HEADERS)
        )
        await resolve_when_pending(app, approved=True)
        await task
    (request,) = conn.messages
    assert request.summary == 'Write {"file_path": "/tmp/a"}'


def test_approvals_bad_token_403(client):
    for headers in [{}, {"X-Voicecode-Token": "wrong"}]:
        response = client.post("/approvals", json=BASH_PAYLOAD, headers=headers)
        assert response.status_code == 403


async def test_approvals_timeout_408(tmp_path):
    app, client = make_async_client(tmp_path)
    app.state.approvals.timeout = 0.05
    async with client:
        response = await client.post("/approvals", json=BASH_PAYLOAD, headers=APPROVAL_HEADERS)
    assert response.status_code == 408
    assert app.state.approvals.pending == {}


def test_static_pwa_with_spa_fallback(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>PWA</html>")
    (web / "app.js").write_text("console.log('hi')")
    app = make_app(tmp_path, Fakes(tmp_path), web_dir=web)
    with TestClient(app) as client:
        assert client.get("/").text == "<html>PWA</html>"
        assert client.get("/app.js").text == "console.log('hi')"
        assert client.get("/some/client/route").text == "<html>PWA</html>"  # SPA fallback
        assert client.get("/api/unknown").status_code == 404  # API paths never fall back


def test_static_missing_web_dir_is_a_404_not_a_crash(tmp_path):
    app = make_app(tmp_path, Fakes(tmp_path), web_dir=tmp_path / "nonexistent")
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
