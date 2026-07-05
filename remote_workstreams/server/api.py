"""REST: pairing, login, credentials, health, and the phone-approval relay endpoint.
The pairing/login shapes are a frozen contract — the PWA builds against them exactly.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from remote_workstreams.server.auth import LoginError, PairingError

router = APIRouter()


class PairStart(BaseModel):
    pin: str


class PairFinish(BaseModel):
    pairing_id: str
    attestation: dict[str, Any]


class LoginFinish(BaseModel):
    login_id: str
    assertion: dict[str, Any]


def require_session(request: Request) -> None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not request.app.state.login.session_ok(value.strip()):
        raise HTTPException(status_code=401, detail="invalid session")


def _rp_id(request: Request) -> str:
    return request.url.hostname or "localhost"  # the MagicDNS name in production


def _origin(request: Request) -> str:
    return request.headers.get("origin") or f"https://{request.url.hostname}"


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.post("/api/pair/start")
async def pair_start(body: PairStart, request: Request) -> dict:
    try:
        pairing_id, options = request.app.state.pairing.start(body.pin, _rp_id(request))
    except PairingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"pairing_id": pairing_id, "registration_options": options}


@router.post("/api/pair/finish")
async def pair_finish(body: PairFinish, request: Request) -> dict:
    try:
        credential_id = request.app.state.pairing.finish(
            body.pairing_id, body.attestation, _origin(request)
        )
    except PairingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # A freshly paired device is logged in — no immediate re-auth.
    return {"credential_id": credential_id, "session_token": request.app.state.login.mint()}


@router.post("/api/login/start")
async def login_start(request: Request) -> dict:
    login_id, options = request.app.state.login.start(_rp_id(request))
    return {"login_id": login_id, "authentication_options": options}


@router.post("/api/login/finish")
async def login_finish(body: LoginFinish, request: Request) -> dict:
    try:
        session_token = request.app.state.login.finish(
            body.login_id, body.assertion, _origin(request)
        )
    except LoginError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"session_token": session_token}


@router.post("/approvals")
async def approvals(request: Request) -> dict:
    """Body is the raw PreToolUse hook JSON, relayed by hooks/ask_phone.py."""
    token = request.headers.get("x-workstreams-token", "")
    if not hmac.compare_digest(token, request.app.state.approvals_token):
        raise HTTPException(status_code=403, detail="bad token")
    payload = await request.json()
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if tool == "Bash":
        summary = str(tool_input.get("command", ""))
    else:
        summary = f"{tool} {json.dumps(tool_input)[:120]}".strip()
    try:
        approved = await request.app.state.approvals.create(
            str(payload.get("session_id", "")), tool, summary
        )
    except TimeoutError:
        raise HTTPException(status_code=408, detail="approval timed out") from None
    return {"decision": "allow" if approved else "deny"}


@router.get("/api/credentials", dependencies=[Depends(require_session)])
async def list_credentials(request: Request) -> dict:
    return {"credentials": [asdict(c) for c in request.app.state.store.list_credentials()]}


@router.post("/api/credentials/{credential_id}/revoke", dependencies=[Depends(require_session)])
async def revoke_credential(credential_id: str, request: Request) -> dict:
    if not request.app.state.store.revoke_credential(credential_id):
        raise HTTPException(status_code=404, detail="unknown credential")
    return {"ok": True}
