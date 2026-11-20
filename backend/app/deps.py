"""Shared by the routers: the one Boson client, the token buckets, rate keys, the access gate and Boson error
mapping (one instance of each, so a test patching deps.<name> reaches every route)."""
import re
import secrets
from pathlib import Path

from fastapi import Header, HTTPException, Request

from . import public_client, users
from .boson import BosonClient, BosonError
from .config import settings
from .ratelimit import TokenBucket, client_ip
from .schema import Book


def _lipsync_dir() -> Path:
    """Lip-sync clips: shared cache/avatar/talk, or the caller's own folder with PER_USER."""
    return users.lipsync_dir()


boson = BosonClient()
token_bucket = TokenBucket(capacity=10, per_seconds=60)
tts_bucket = TokenBucket(capacity=120, per_seconds=60)   # only cache misses (real Boson calls) consume this
clone_bucket = TokenBucket(capacity=5, per_seconds=60)
answer_bucket = TokenBucket(capacity=20, per_seconds=60)  # every /api/answer call spends Gemini credit
retrieve_bucket = TokenBucket(capacity=30, per_seconds=60)  # vertex retrieval spends embedding + search credit
translate_bucket = TokenBucket(capacity=60, per_seconds=60)  # paragraph translations that miss the cache (Gemini credit)
upload_bucket = TokenBucket(capacity=6, per_seconds=60)  # uploads per IP (a multi-file drop; UPLOAD_QUEUE_MAX)
describe_bucket = TokenBucket(capacity=12, per_seconds=60)  # per IP / account: each may cost one Gemini Flash call
avatar_bucket = TokenBucket(capacity=12, per_seconds=60)  # every render spends Boson avatar credit
live_avatar_bucket = TokenBucket(capacity=6, per_seconds=60)  # session starts per IP / account (each holds a paid slot)
face_bucket = TokenBucket(capacity=10, per_seconds=60)  # uploads write to disk
face_detect_bucket = TokenBucket(capacity=12, per_seconds=60)  # per IP / account: each may cost one Gemini Flash call


def rate_key(request: Request) -> str:
    """Rate-limit key: the signed-in account with PER_USER, else the client address."""
    return users.rate_key(request, client_ip(request))


def require_access(x_talkback_token: str | None = Header(default=None)) -> None:
    """Opt-in gate for endpoints that spend Boson credit: set TALKBACK_ACCESS_TOKEN for any public deployment."""
    expected = settings.access_token
    if expected and not secrets.compare_digest(x_talkback_token or "", expected):
        raise HTTPException(401, "Missing or invalid access token.")


def boson_http_error(e: BosonError | RuntimeError) -> HTTPException:
    """Turn Boson failures into readable API errors. insufficient_quota is a billing problem, not a retry."""
    if isinstance(e, RuntimeError):
        return HTTPException(503, str(e))
    if e.type == "insufficient_quota":
        return HTTPException(402, f"Boson billing refused the request ({e.code or 'insufficient_quota'}): {e.message}")
    if e.type == "rate_limit_error":
        return HTTPException(429, "Boson is rate limiting requests; try again shortly.")
    if e.status in (400, 403):
        return HTTPException(502, f"Boson rejected the request: {e.message}")
    return HTTPException(502, f"Boson error ({e.type}): {e.message}")


def _public_answers() -> bool:
    """Typed/Gemini answers go through the public API: it is configured and this machine has no LLM of its own."""
    return settings.answer_backend is None and settings.retrieval_backend != "vertex" and public_client.active()


def answer_backend() -> str:
    """"gemini" when /api/answer writes the answers (locally, or through the public API while it answers), else "realtime"."""
    if _public_answers() and public_client.answers_available():
        return "gemini"
    return settings.effective_answer_backend


# GET /api/books/{id} serves these; a delete (processing._forget_book) drops its entry.
_book_bodies: dict[str, tuple[Book, bytes, bytes]] = {}  # book id -> (book object, json, gzipped json)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")  # avatar folders and cast settings: a book id / chunk id from the URL
