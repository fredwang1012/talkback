"""Live faces (#140): Boson avatar sessions in a LiveKit room.
Logs as "app.main" (these lines were logged from main.py before the routers split, so they read the same)."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import deps, live_avatar, users
from ..boson import BosonError
from ..config import settings


router = APIRouter()


_live_lock = asyncio.Lock()


def _live_owner(request: Request) -> str:
    """Whose live face this is: the account, or the client on a local run. Anonymous visitors of a PER_USER deploy all
    share the namespace "public", so they cannot hold one (one could end or replace another's)."""
    ident = users.current()
    if ident.ns == users.PUBLIC_NS:
        raise HTTPException(403, "Sign in to use a live face.")
    return ident.ns if ident.ns else deps.rate_key(request)


@router.get("/api/live-avatar", dependencies=[Depends(deps.require_access)])
async def live_avatar_info():
    """Whether live faces are available here, and which (Boson's presets: GET /v1/avatars)."""
    if not settings.live_avatar_enabled:
        return {"enabled": False, "avatars": []}
    try:
        found = await deps.boson.list_avatars()
    except (BosonError, RuntimeError):
        return {"enabled": False, "avatars": []}
    return {"enabled": True, "avatars": [{"id": a["avatar_id"], "name": str(a.get("name") or a["avatar_id"])} for a in found]}


class LiveAvatarStart(BaseModel):
    avatar_id: str = Field(..., min_length=1, max_length=256)


@router.post("/api/live-avatar/sessions", dependencies=[Depends(deps.require_access)])
async def start_live_avatar(req: LiveAvatarStart, request: Request):
    """Start a live face: Boson's avatar joins a fresh LiveKit room; the reply's browser joins with the token returned.
    One session per account (an older one is released first). 503 `live_unavailable` means: keep the clips."""
    if not settings.live_avatar_enabled:
        raise HTTPException(404, "Live faces are not set up here.")
    if not deps.live_avatar_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many live face starts; wait a moment.")
    owner = _live_owner(request)
    for old in live_avatar.held_by(owner):   # one per account: an older one is released first
        if old.id.startswith(live_avatar.PENDING):
            raise HTTPException(409, "A live face is already starting.")
        try:
            await deps.boson.release_avatar_session(old.id)
        except (BosonError, RuntimeError):
            logging.getLogger("app.main").warning("live avatar: could not release an older session")
            continue   # still held: it keeps counting until it expires
        live_avatar.pop(old.id, owner)
    if live_avatar.count() >= settings.live_avatar_max_sessions:
        await _drop_ended_live_sessions()   # sessions that ended without a release (closed tab, avatar left)
    async with _live_lock:  # only the in-memory checks and a reservation: parallel starts cannot pass either cap together
        if live_avatar.held_by(owner):   # still starting, or an older one that would not release
            raise HTTPException(409, "A live face is already running or starting for this account.")
        if live_avatar.count() >= settings.live_avatar_max_sessions:
            raise HTTPException(503, detail={"code": "live_unavailable", "message": "Live faces are busy; using clips."})
        slot = live_avatar.reserve(owner)
    plan = live_avatar.plan(owner, req.avatar_id)
    try:
        made = await deps.boson.create_avatar_session(plan["body"])
    except BosonError as e:
        live_avatar.pop(slot, owner)
        if e.status in (429, 503) and e.type != "insufficient_quota":
            raise HTTPException(503, detail={"code": "live_unavailable", "message": "Live faces are busy; using clips."})
        if e.status == 404:
            raise HTTPException(404, "That face is not available for live sessions.")
        raise deps.boson_http_error(e)
    except BaseException as e:
        live_avatar.pop(slot, owner)
        if isinstance(e, RuntimeError):
            raise deps.boson_http_error(e)
        raise
    live_avatar.pop(slot, owner)
    live_avatar.add(live_avatar.Session(made["id"], owner, plan["room"], req.avatar_id))
    return {"session": made["id"], "url": settings.livekit_url, "token": plan["token"], "identity": plan["identity"],
            "avatar_identity": made.get("avatar_identity") or live_avatar.AVATAR_IDENTITY,
            "expires_at": made.get("expires_at"), "max_seconds": settings.live_avatar_max_seconds}


async def _drop_ended_live_sessions() -> None:
    """Ask Boson about each held session and forget the ones it has ended, so a tab closed without a release does not
    keep "Live faces are busy" up until the session's cap."""
    for s in live_avatar.snapshot():
        if s.id.startswith(live_avatar.PENDING):
            continue
        try:
            status = await deps.boson.get_avatar_session(s.id)
        except (BosonError, RuntimeError):
            continue
        if status in ("terminated", "gone"):
            live_avatar.pop(s.id, s.owner)


@router.delete("/api/live-avatar/sessions/{session_id}", dependencies=[Depends(deps.require_access)])
async def stop_live_avatar(session_id: str, request: Request):
    """Release a live face (only the account that started it). Boson also ends it when everyone leaves the room."""
    s = live_avatar.pop(session_id, _live_owner(request))
    if s is None:
        raise HTTPException(404, "No such live session.")
    try:
        await deps.boson.release_avatar_session(session_id)
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    return {"released": session_id}
