"""Realtime key minting."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import deps
from ..boson import BosonError
from ..gcp import usage


router = APIRouter()


@router.post("/api/realtime/token", dependencies=[Depends(deps.require_access)])
async def realtime_token(request: Request):
    """Mint a fresh ephemeral Realtime key per call (never cached). The real API key never leaves the server."""
    if not deps.token_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many connection attempts; wait a moment.")
    try:
        secret = await deps.boson.mint_client_secret(600)
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    usage.record("boson_realtime_session", "boson-realtime", sessions=1)
    return secret
