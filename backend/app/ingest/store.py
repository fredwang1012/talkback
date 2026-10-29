"""The book folder on disk: ids, status.json, the kept upload, deletion, and duplicate detection (#82)."""
import hashlib
import json
import re
import secrets
import shutil
import threading
import unicodedata
from pathlib import Path

from .. import books
from ..config import settings


def make_book_dir(title: str) -> tuple[str, Path]:
    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:40].strip("-") or "book"
    books_dir = settings.data_dir / "books"
    books_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(10):
        book_id = f"u-{slug}-{secrets.token_hex(4)}"
        path = books_dir / book_id
        try:
            path.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return book_id, path
    raise RuntimeError("could not allocate a book id")


def discard_book(book_id: str) -> None:
    """Remove everything but status.json after a failed ingest."""
    d = settings.data_dir / "books" / book_id
    shutil.rmtree(d / "index", ignore_errors=True)
    shutil.rmtree(d / "figures", ignore_errors=True)
    for name in ("book.json", "book.json.tmp"):
        (d / name).unlink(missing_ok=True)
    for tmp in d.glob("book.json.*.tmp"):   # books.save_book's per-call temp files (only left by a crash)
        tmp.unlink(missing_ok=True)


KIND_KEYS = ("kind", "kind_detected", "enrich_retries")   # carried over from the previous status


def write_status(book_id: str, status: str, message: str | None = None, enrich: dict | None = None,
                 index: dict | None = None, **kind_info) -> dict:
    """status.json: {status, message?, enrich?, index?, kind?, kind_detected?}.

    `index` is the Vertex indexing progress {state: running|done|error, done, total, message?}; absent for books
    that never needed it (local retrieval, seeded books).

    `enrich` is the novel enrichment sub-status (see enrich/pipeline.py). `kind`/`kind_detected` are carried over from
    the previous status unless given.
    """
    prev = read_status(book_id) or {}
    out = {"status": status} | ({"message": message} if message else {}) | ({"enrich": enrich} if enrich else {})
    index = index if index is not None else prev.get("index")  # carried over so enrich/kind updates keep it
    if index:
        out["index"] = index
    for k in KIND_KEYS:
        v = kind_info[k] if k in kind_info else prev.get(k)
        if v is not None:
            out[k] = v
    d = settings.data_dir / "books" / book_id
    d.mkdir(parents=True, exist_ok=True)
    _write_status_file(d / "status.json", out)
    return out


def _write_status_file(path: Path, st: dict) -> None:
    """status.json is small and polled by the UI; a Windows PermissionError (another handle on it) is retried briefly."""
    books.retry_permission(lambda: path.write_text(json.dumps(st), encoding="utf-8"))


def save_status(book_id: str, st: dict) -> bool:
    """Write a status snapshot as is; never recreates a book folder that is gone (a deleted book). True if written."""
    d = settings.data_dir / "books" / book_id
    if not d.is_dir():
        return False
    _write_status_file(d / "status.json", st)
    return True


def read_status(book_id: str) -> dict | None:
    path = settings.data_dir / "books" / book_id / "status.json"
    if path.is_file():
        try:
            return json.loads(books.read_bytes(path))
        except (ValueError, OSError):   # half-written, or gone between is_file and the read
            return None
    return None


def upload_path(book_id: str, ext: str) -> Path:
    return settings.data_dir / "uploads" / f"{book_id}{ext}"


def delete_book_files(book_id: str, ident=None) -> list[str]:
    """Remove an uploaded book from disk: the book folder (book.json first, so it leaves the library at once), the
    original upload, avatar clips, portraits and the caller's cast file. Returns what could not be removed.

    Robust on a GCS FUSE mount, where removing a tree can fail part-way: each part is retried once and a failure never
    stops the others. Only the folders named after this exact book id are touched (the caller validates the id).
    """
    from .. import users
    root = settings.data_dir
    d = root / "books" / book_id
    for name in ("book.json", "book.json.tmp"):   # hide it first: a half-deleted book must not stay listed
        try:
            (d / name).unlink(missing_ok=True)
        except OSError:
            pass
    targets = [d, root / "cache" / "avatar" / book_id, root / "portraits" / book_id,
               *sorted((root / "uploads").glob(f"{book_id}.*")), users.cast_json(book_id)]
    left: list[str] = []
    for t in targets:
        for _ in range(2):
            try:
                if t.is_dir():
                    shutil.rmtree(t, ignore_errors=True)
                else:
                    t.unlink(missing_ok=True)
            except OSError:
                pass
            if not t.exists():
                break
        else:
            left.append(t.relative_to(root).as_posix())
    return left


# ---- duplicate uploads (#82): sha256 of the uploaded bytes -> book id, one map per account ----

_hashes_lock = threading.Lock()   # read-modify-write of upload_hashes.json (uploads record from worker threads)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_hashes(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def record_upload_hash(digest: str, book_id: str, ident=None) -> None:
    """Remember that this account's file `digest` is book `book_id` (atomic write; a newer upload replaces it)."""
    from .. import users
    path = users.upload_hashes_json(ident)
    with _hashes_lock:
        hashes = _read_hashes(path)
        hashes[digest] = book_id
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(hashes), encoding="utf-8")
        tmp.replace(path)


def find_upload(digest: str, ident=None) -> tuple[str, str, str | None] | None:
    """The account's existing book for this file: (book id, "ready" | "deleting", title), or None.

    "deleting" means a delete tombstone is on disk (the caller decides whether that delete is still running). A folder
    with neither book.json nor a tombstone is a failed or interrupted ingest: no book to point at, so None.
    """
    from .. import users
    book_id = _read_hashes(users.upload_hashes_json(ident)).get(digest)
    if not book_id or not books._ID.match(book_id) or not users.can_see_book(book_id, ident):
        return None
    d = settings.data_dir / "books" / book_id
    if (d / "deleting.json").is_file():
        return book_id, "deleting", None
    if (d / "book.json").is_file():
        book = books.load_book(book_id)
        return (book_id, "ready", book.title) if book else None
    return None
