"""REST: pairing, sessions, credentials, health. The pairing and session shapes
are a frozen contract — the PWA unit builds against them exactly.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from voicecode.server.auth import PairingError, credential_ok

router = APIRouter()


class PairStart(BaseModel):
    token: str
    pin: str


class PairFinish(BaseModel):
    pairing_id: str
    attestation: dict[str, Any]


def require_credential(request: Request) -> None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credential_ok(request.app.state.store, value.strip()):
        raise HTTPException(status_code=401, detail="invalid credential")


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.post("/api/pair/start")
async def pair_start(body: PairStart, request: Request) -> dict:
    rp_id = request.url.hostname or "localhost"  # the MagicDNS name in production
    try:
        pairing_id, options = request.app.state.pairing.start(body.token, body.pin, rp_id)
    except PairingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"pairing_id": pairing_id, "registration_options": options}


@router.post("/api/pair/finish")
async def pair_finish(body: PairFinish, request: Request) -> dict:
    origin = request.headers.get("origin") or f"https://{request.url.hostname}"
    try:
        credential, credential_id = request.app.state.pairing.finish(
            body.pairing_id, body.attestation, origin
        )
    except PairingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"credential": credential, "credential_id": credential_id}


@router.get("/api/sessions", dependencies=[Depends(require_credential)])
async def list_sessions(request: Request) -> dict:
    return {"sessions": [s.model_dump() for s in request.app.state.store.list_sessions()]}


@router.get("/api/credentials", dependencies=[Depends(require_credential)])
async def list_credentials(request: Request) -> dict:
    return {"credentials": [asdict(c) for c in request.app.state.store.list_credentials()]}


@router.post("/api/credentials/{credential_id}/revoke", dependencies=[Depends(require_credential)])
async def revoke_credential(credential_id: str, request: Request) -> dict:
    if not request.app.state.store.revoke_credential(credential_id):
        raise HTTPException(status_code=404, detail="unknown credential")
    return {"ok": True}
