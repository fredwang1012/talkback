"""Per-account separation for the live Cloud Run deploy (PER_USER=true, behind Google sign-in via IAP).

The user is identified by VERIFYING the IAP JWT (header x-goog-iap-jwt-assertion); the plain
x-goog-authenticated-user-email header is never trusted. No valid JWT -> the shared anonymous namespace "public".
With PER_USER=false every path helper returns today's shared location, so local runs are unchanged.
"""
import asyncio
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
from google.auth.exceptions import TransportError

from .config import settings

log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
PUBLIC_NS = "public"
RESULT_TTL = 300.0
CERTS_TTL = 3600.0
CERTS_FAIL_TTL = 30.0   # after a failed cert fetch, do not hit the network again for this long
REFETCH_MIN_INTERVAL = 30.0   # an unknown key id may trigger a cert refresh at most this often


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
        self.inner, self.cache, self.failed_at, self.refreshed_at = inner, {}, {}, -1e9

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kw):
        now = time.monotonic()
        hit = self.cache.get(url)
        if hit and now - hit[0] < CERTS_TTL:
            return hit[1]
        if now - self.failed_at.get(url, -1e9) < CERTS_FAIL_TTL:
            raise TransportError("IAP certs unavailable (recent fetch failed)")
        try:
            resp = self.inner(url, method=method, body=body, headers=headers, timeout=timeout or 10, **kw)
        except Exception as e:
            self.failed_at[url] = now
            raise TransportError("IAP cert fetch failed") from e
        if getattr(resp, "status", 200) != 200:
            self.failed_at[url] = now
        else:
            self.cache[url] = (now, resp)
        return resp

    def refresh(self) -> bool:
        """Drop the cached certs so the next call refetches (rate limited). True when it did."""
        now = time.monotonic()
        if now - self.refreshed_at < REFETCH_MIN_INTERVAL:
            return False
        self.refreshed_at = now
        self.cache.clear()
        return True


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
        try:
            claims = id_token.verify_token(token, _transport(), audience=settings.iap_audience, certs_url=IAP_CERTS_URL)
        except ValueError as e:
            tr = _transport()
            if "key id" in str(e) and hasattr(tr, "refresh") and tr.refresh():   # Google rotated its keys
                claims = id_token.verify_token(token, tr, audience=settings.iap_audience, certs_url=IAP_CERTS_URL)
            else:
                raise
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
    """LOCAL without PER_USER. No IAP header (the gate is off) -> anonymous. A header that fails verification
    is an error (401), never a silent downgrade to anonymous."""
    if not settings.per_user:
        return LOCAL
    token = headers.get(IAP_HEADER, "")
    if not token:
        return ANONYMOUS
    email = verify_email(token)
    if not email:
        raise HTTPException(401, "Sign-in could not be verified.")
    return Identity(email, ns_for(email))


def identify(request: Request) -> Identity:
    ident = getattr(request.state, "identity", None)
    return ident if ident is not None else identify_headers(request.headers)


async def identify_async(headers) -> Identity:
    """identify_headers off the event loop (a first verification fetches Google's keys over the network)."""
    return await asyncio.to_thread(identify_headers, headers)


async def bind_identity(request: Request) -> None:
    """App-wide dependency: async so the contextvar lands in the request's context (sync endpoints copy it)."""
    ident = await identify_async(request.headers)
    request.state.identity = ident
    _current.set(ident)


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


def lipsync_dir(ident: Identity | None = None) -> Path:
    ident = ident or current()
    return user_root(ident) / "lipsync" if ident.ns else settings.data_dir / "cache" / "avatar" / "talk"


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


def require_account() -> Identity:
    """Destructive actions: with PER_USER on, only a signed-in account (never the shared anonymous namespace) may run
    them. With PER_USER off (a single-user local run) they stay allowed behind the usual access-code gate."""
    ident = current()
    if ident.ns and not ident.email:
        raise HTTPException(403, "Sign in to delete things.")
    return ident


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
    _migrate_faces(data_dir, root, copy, acts, dry_run)
    for clip in sorted((data_dir / "cache" / "avatar" / "talk").glob("*.mp4")):
        copy(clip, root / "lipsync" / clip.name)
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


def _migrate_faces(data_dir: Path, root: Path, copy, acts: list[str], dry_run: bool) -> None:
    """faces.json + portraits/faces/* -> the owner. Also folds a legacy voice_faces.json (voice id -> file in
    portraits/voices/, the pre-library binding) that was never folded into faces.json, so the owner keeps the
    face <-> voice binding. Voices that already have a face are left alone."""
    faces_src = data_dir / "portraits" / "faces"
    src_json, dst_json = data_dir / "faces.json", root / "faces.json"
    legacy = data_dir / "voice_faces.json"
    try:
        base = dst_json if dst_json.is_file() else src_json
        entries = json.loads(base.read_text(encoding="utf-8")) if base.is_file() else {}
        changed = not dst_json.is_file() and src_json.is_file()
        if not dst_json.is_file():
            for e in entries.values():
                stem = Path(e["file"]).stem
                for name in (e["file"], f"{stem}-talk.mp4", f"{stem}-idle.mp4"):
                    copy(faces_src / name, root / "faces" / name)
        if legacy.is_file():
            names = {}
            vj = data_dir / "voices.json"
            if vj.is_file():
                names = {v: n for n, v in json.loads(vj.read_text(encoding="utf-8")).items()}
            have = {e.get("voice") for e in entries.values()}
            for voice, fname in json.loads(legacy.read_text(encoding="utf-8")).items():
                src = data_dir / "portraits" / "voices" / fname
                if voice in have or not src.is_file():
                    continue
                fid = "v" + hashlib.sha256(voice.encode()).hexdigest()[:16]
                dest = f"{fid}{src.suffix}"
                copy(src, root / "faces" / dest)
                for kind in ("talk", "idle"):
                    copy(src.with_name(f"{src.stem}-{kind}.mp4"), root / "faces" / f"{fid}-{kind}.mp4")
                entries[fid] = {"name": names.get(voice, voice), "file": dest, "voice": voice}
                acts.append(f"bind legacy voice_faces.json {voice} -> {fid}")
                changed = True
        if changed:
            acts.append(f"write {dst_json.relative_to(data_dir).as_posix()}")
            if not dry_run:   # written last: its presence marks the faces as migrated
                dst_json.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst_json.with_name(dst_json.name + ".tmp")
                tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
                tmp.replace(dst_json)
    except Exception as e:
        acts.append(f"skip faces: {type(e).__name__}")


def migrate_on_startup() -> None:
    """PER_USER start-up hook. Never raises; idempotent because it only copies files that are missing."""
    if not settings.per_user or not settings.owner_email_list:
        return
    try:
        acts = migrate_to_owner(settings.data_dir, settings.owner_email_list[0])
        log.info("per-user migration: %d action(s)", len(acts))
    except Exception:
        log.exception("per-user migration failed; continuing")
