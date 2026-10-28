"""Live avatar sessions (#140): a Boson preset face rendered live into a LiveKit Cloud room.

The browser joins the room as the publisher, streams each reply's 24 kHz PCM to the avatar over the LiveKit Agents
avatar audio protocol (byte stream topic `lk.audio_stream`, RPC `lk.clear_buffer`), and plays the avatar's own video
and audio tracks, so voice and lips come from one stream and stay in sync. This module mints the two LiveKit tokens
(HS256 JWTs signed with LIVEKIT_API_SECRET, no SDK needed) and keeps track of the sessions this server holds, so each
account has at most one and the server at most `live_avatar_max_sessions`.

Boson only offers its presets (`GET /v1/avatars`: Claire, Emma, James) for live sessions; there is no API to make an
avatar from a photo, so faces of our own keep the per-sentence lip-sync clips.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from .config import settings

AVATAR_IDENTITY = "boson-avatar"
TOKEN_SLACK_S = 300   # the avatar's token outlives the session cap by this much


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_token(identity: str, room: str, ttl_s: int, *, publish: bool, subscribe: bool, sources: list[str] | None = None,
               kind: str | None = None, attributes: dict[str, str] | None = None, now: float | None = None) -> str:
    """A LiveKit access token (JWT, HS256): `iss` the API key, `sub` the identity, a `video` grant for one room."""
    now = int(now if now is not None else time.time())
    video: dict = {"room": room, "roomJoin": True, "canPublish": publish, "canSubscribe": subscribe, "canPublishData": True}
    if sources:
        video["canPublishSources"] = sources
    claims: dict = {"iss": settings.livekit_api_key, "sub": identity, "nbf": now - 10, "exp": now + ttl_s, "video": video}
    if kind:
        claims["kind"] = kind
    if attributes:
        claims["attributes"] = attributes
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(settings.livekit_api_secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


@dataclass
class Session:
    id: str
    owner: str            # rate key / account namespace that started it
    room: str
    avatar_id: str
    started: float = field(default_factory=time.time)


_sessions: dict[str, Session] = {}


def new_room() -> tuple[str, str]:
    """(room name, the reader's identity): random, so one account's rooms are never guessable by another."""
    tag = secrets.token_hex(6)
    return f"talkback-{tag}", f"reader-{tag}"


def plan(owner: str, avatar_id: str) -> dict:
    """The room, the two tokens and Boson's session body for a new session (nothing is called here)."""
    room, reader = new_room()
    cap = settings.live_avatar_max_seconds
    avatar_token = mint_token(AVATAR_IDENTITY, room, cap + TOKEN_SLACK_S, publish=True, subscribe=False,
                              sources=["camera", "microphone"], kind="agent", attributes={"lk.publish_on_behalf": reader})
    reader_token = mint_token(reader, room, cap + TOKEN_SLACK_S, publish=True, subscribe=True)
    body = {"model": "higgs-avatar", "avatar_id": avatar_id, "max_duration_seconds": cap,
            "transport": {"type": "livekit", "url": settings.livekit_url, "room_name": room,
                          "participant_token": avatar_token, "participant_identity": AVATAR_IDENTITY,
                          "publisher_identity": reader, "audio_source": "data_stream"}}
    return {"room": room, "identity": reader, "token": reader_token, "body": body}


PENDING = "pending-"


def reserve(owner: str) -> str:
    """Hold a slot while Boson starts the session (counted like a session; replaced or dropped right after)."""
    sid = PENDING + secrets.token_hex(6)
    _sessions[sid] = Session(sid, owner, "", "")
    return sid


def held_by(owner: str) -> list[Session]:
    expire()
    return [s for s in _sessions.values() if s.owner == owner]


def count() -> int:
    expire()
    return len(_sessions)


def add(s: Session) -> None:
    _sessions[s.id] = s


def pop(session_id: str, owner: str) -> Session | None:
    s = _sessions.get(session_id)
    if s is None or s.owner != owner:
        return None
    return _sessions.pop(session_id)


def expire(now: float | None = None) -> None:
    """Forget sessions past their cap (Boson ends them itself at expires_at)."""
    now = now if now is not None else time.time()
    for sid in [k for k, s in _sessions.items() if now - s.started > settings.live_avatar_max_seconds + TOKEN_SLACK_S]:
        _sessions.pop(sid, None)
