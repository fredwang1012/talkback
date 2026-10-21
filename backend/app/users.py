"""Per-account separation for the live Cloud Run deploy (PER_USER=true, behind Google sign-in via IAP).

The user is identified by VERIFYING the IAP JWT (header x-goog-iap-jwt-assertion); the plain
x-goog-authenticated-user-email header is never trusted. No valid JWT -> the shared anonymous namespace "public".
With PER_USER=false every path helper returns today's shared location, so local runs are unchanged.
"""
import fnmatch
import hashlib
import json
import logging
import shutil
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from .config import settings

log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
PUBLIC_NS = "public"
RESULT_TTL = 300.0
CERTS_TTL = 3600.0


@dataclass(frozen=True)
class Identity:
    email: str | None = None
    ns: str = ""          # "" when PER_USER is off; "public" for anonymous; else sha256(email)[:16]

    @property
    def is_owner(self) -> bool:
        return bool(self.email) and self.email in settings.owner_email_list


LOCAL = Identity()
ANONYMOUS = Identity(None, PUBLIC_NS)
_current: ContextVar[Identity] = ContextVar("talkback_identity", default=LOCAL)


def ns_for(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def owner_ns() -> str | None:
    owners = settings.owner_email_list
    return ns_for(owners[0]) if owners else None


# ---- JWT verification ----

class _CertsRequest:
    """google-auth transport that caches the IAP public keys for an hour (verify_token would refetch every call)."""

    def __init__(self, inner):
        self.inner, self.cache = inner, {}

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kw):
        hit = self.cache.get(url)
        if hit and time.monotonic() - hit[0] < CERTS_TTL:
            return hit[1]
        resp = self.inner(url, method=method, body=body, headers=headers, timeout=timeout or 10, **kw)
        if getattr(resp, "status", 200) == 200:
            self.cache[url] = (time.monotonic(), resp)
        return resp


_certs_request: _CertsRequest | None = None


def _transport():
    """The google-auth request callable (tests replace this with a fake that serves test certs)."""
    global _certs_request
    if _certs_request is None:
        from google.auth.transport import requests as ga_requests
        _certs_request = _CertsRequest(ga_requests.Request())
    return _certs_request


_results: dict[str, tuple[float, str | None]] = {}   # sha256(token) -> (valid until, email or None)
_results_lock = threading.Lock()


def verify_email(token: str) -> str | None:
    """Verified email of an IAP JWT, or None (missing/invalid/expired/wrong audience). Never logs the token."""
    if not token or not settings.iap_audience:
        return None
    key = hashlib.sha256((settings.iap_audience + "\0" + token).encode()).hexdigest()
    now = time.monotonic()
    with _results_lock:
        hit = _results.get(key)
    if hit and hit[0] > now:
        return hit[1]
    email, ttl = None, 30.0   # failures are cached briefly too, so junk tokens cannot hammer the verifier
    try:
        from google.oauth2 import id_token
        claims = id_token.verify_token(token, _transport(), audience=settings.iap_audience, certs_url=IAP_CERTS_URL)
        if claims.get("iss") != IAP_ISSUER:
            raise ValueError("bad issuer")
        mail = claims.get("email")
        if not isinstance(mail, str) or "@" not in mail:
            raise ValueError("no email")
        email = mail.strip().lower()
        ttl = min(RESULT_TTL, max(1.0, float(claims.get("exp", 0)) - time.time()))
    except Exception as e:
        log.warning("IAP JWT rejected: %s", type(e).__name__)
    with _results_lock:
        if len(_results) > 1000:
            _results.clear()
        _results[key] = (now + ttl, email)
    return email


def identify_headers(headers) -> Identity:
    if not settings.per_user:
        return LOCAL
    email = verify_email(headers.get(IAP_HEADER, ""))
    return Identity(email, ns_for(email)) if email else ANONYMOUS


def identify(request: Request) -> Identity:
    return identify_headers(request.headers)


async def bind_identity(request: Request) -> None:
    """App-wide dependency: async so the contextvar lands in the request's context (sync endpoints copy it)."""
    _current.set(identify(request))


def current() -> Identity:
    return _current.get()


def rate_key(request: Request, fallback: str) -> str:
    ident = identify(request)
    return f"user:{ident.ns}" if ident.email else fallback


# ---- namespaced paths (shared locations when PER_USER is off) ----

def user_root(ident: Identity | None = None) -> Path:
    ident = ident or current()
    return settings.data_dir / "users" / ident.ns if ident.ns else settings.data_dir


def voices_json(ident: Identity | None = None) -> Path:
    return user_root(ident) / "voices.json"


def faces_json(ident: Identity | None = None) -> Path:
    return user_root(ident) / "faces.json"


def faces_dir(ident: Identity | None = None) -> Path:
    ident = ident or current()
    return user_root(ident) / "faces" if ident.ns else settings.data_dir / "portraits" / "faces"


def cast_json(book_id: str, ident: Identity | None = None) -> Path:
    ident = ident or current()
    if not ident.ns:
        return settings.data_dir / "books" / book_id / "cast_settings.json"
    return user_root(ident) / "cast" / f"{book_id}.json"


# ---- book access ----

def _owner_marker(book_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "owner.txt"


def claim_book(book_id: str, ident: Identity | None = None) -> None:
    ident = ident or current()
    if ident.ns:
        _owner_marker(book_id).write_text(ident.ns, encoding="utf-8")


def book_owner_ns(book_id: str) -> str | None:
    try:
        return _owner_marker(book_id).read_text(encoding="utf-8").strip() or None
    except OSError:
        return owner_ns()   # an upload from before accounts belongs to the owner


def is_owner_only(book_id: str) -> bool:
    return any(fnmatch.fnmatchcase(book_id, g.strip()) for g in settings.owner_only_books.split(",") if g.strip())


def can_see_book(book_id: str, ident: Identity | None = None) -> bool:
    ident = ident or current()
    if not ident.ns:   # PER_USER off
        return True
    if is_owner_only(book_id):
        return ident.is_owner
    if book_id.startswith("u-"):
        return book_owner_ns(book_id) == ident.ns
    return True


def guard_book(book_id: str) -> None:
    """404 (never 403: do not reveal that it exists) when the caller may not see this book."""
    if not can_see_book(book_id):
        raise HTTPException(404, "Book not found")


# ---- migration of the pre-accounts shared data to the owner ----

def migrate_to_owner(data_dir: Path, owner_email: str, dry_run: bool = False, say=log.info) -> list[str]:
    """Copy the shared voices.json / faces.json / portraits/faces / cast_settings.json to the owner's namespace and
    mark ownerless u-* uploads as the owner's. Copies only, never overwrites, never deletes; idempotent."""
    ns = ns_for(owner_email)
    root = data_dir / "users" / ns
    acts: list[str] = []

    def copy(src: Path, dst: Path) -> None:
        if src.is_file() and not dst.exists():
            acts.append(f"copy {src.relative_to(data_dir).as_posix()} -> {dst.relative_to(data_dir).as_posix()}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    copy(data_dir / "voices.json", root / "voices.json")
    faces_src = data_dir / "portraits" / "faces"
    fj = data_dir / "faces.json"
    if fj.is_file() and not (root / "faces.json").exists():
        try:
            for e in json.loads(fj.read_text(encoding="utf-8")).values():
                stem = Path(e["file"]).stem
                for name in (e["file"], f"{stem}-talk.mp4", f"{stem}-idle.mp4"):
                    copy(faces_src / name, root / "faces" / name)
            copy(fj, root / "faces.json")   # written last: its presence marks the faces as migrated
        except Exception as e:
            acts.append(f"skip faces: {type(e).__name__}")
    books = data_dir / "books"
    for d in sorted(books.glob("*")) if books.is_dir() else []:
        copy(d / "cast_settings.json", root / "cast" / f"{d.name}.json")
        if d.name.startswith("u-") and (d / "book.json").is_file() and not (d / "owner.txt").exists():
            acts.append(f"owner books/{d.name}")
            if not dry_run:
                (d / "owner.txt").write_text(ns, encoding="utf-8")
    for a in acts:
        say("per-user migration: %s%s", "(dry run) " if dry_run else "", a)
    return acts


def migrate_on_startup() -> None:
    """PER_USER start-up hook. Never raises; idempotent because it only copies files that are missing."""
    if not settings.per_user or not settings.owner_email_list:
        return
    try:
        acts = migrate_to_owner(settings.data_dir, settings.owner_email_list[0])
        log.info("per-user migration: %d action(s)", len(acts))
    except Exception:
        log.exception("per-user migration failed; continuing")
