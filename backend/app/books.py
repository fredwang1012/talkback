"""Read books produced by the preprocess pipeline from data/books/<id>/book.json."""
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait

from .config import settings
from .schema import Book
from .ttschunks import CHUNK_RULES_VERSION, needs_rechunk, rechunk_book

log = logging.getLogger(__name__)

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _books_dir():
    return settings.data_dir / "books"


PORTRAIT_EXTS = ("png", "jpg", "jpeg", "webp")


def portrait_url(book_id: str, name: str) -> str | None:
    """Portraits are optional images at data/portraits/<book_id>/<character_id|narrator|cover>.<ext>."""
    for ext in PORTRAIT_EXTS:
        if (settings.data_dir / "portraits" / book_id / f"{name}.{ext}").is_file():
            return f"/portraits/{book_id}/{name}.{ext}"
    return None


_cache: dict[str, tuple[tuple[int, int], Book]] = {}


def load_book(book_id: str) -> Book | None:
    """Parsed book, cached until book.json changes (a novel's file is several MB). Callers must not mutate it."""
    if not _ID.match(book_id):  # ids come from the URL: never let them walk the filesystem
        return None
    path = _books_dir() / book_id / "book.json"
    try:
        st = path.stat()
    except OSError:
        return None
    key, stamp = str(path.resolve()), (st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    for c in book.characters:
        c.portrait = c.portrait or portrait_url(book_id, c.id)
    if book.narrator:
        book.narrator.portrait = book.narrator.portrait or portrait_url(book_id, "narrator")
    _cache[key] = (stamp, book)
    if settings.auto_rechunk and needs_rechunk(book):
        _schedule_upgrade(key, path, stamp)
    return book


# Auto-upgrade of books whose tts_chunks were built with older chunk rules (ttschunks.CHUNK_RULES_VERSION).
# Re-chunking HP5 (18k chunks) takes ~2 s, so it runs on one background thread and requests keep the old chunks
# until the new book.json is saved; the next load_book sees the new mtime and serves it (main._book_bodies follows).
_upgrade_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rechunk")
_upgrade_lock = threading.Lock()
_upgrading: dict[str, Future] = {}   # resolved book.json path -> running / queued upgrade
_upgrade_failed: dict[str, tuple[int, int]] = {}   # path -> file stamp that failed: not retried until the file changes


def _stamp(path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _schedule_upgrade(key: str, path, stamp: tuple[int, int]) -> None:
    with _upgrade_lock:
        if key in _upgrading or _upgrade_failed.get(key) == stamp:
            return
        _upgrading[key] = _upgrade_pool.submit(_upgrade, key, path, stamp)


def _upgrade(key: str, path, stamp: tuple[int, int]) -> bool:
    """Re-chunk one book.json with the current rules, keeping segment styles, and save it atomically. Works on a fresh
    parse (never the cached object, which requests share). Skips the write if the file changed since `stamp` (another
    writer won; its next load schedules again). Any failure leaves the old file in place and is logged."""
    try:
        if _stamp(path) != stamp:
            return False
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        if not needs_rechunk(book):
            return False
        before = book.chunk_rules
        rechunk_book(book)
        if _stamp(path) != stamp:
            return False
        save_book(path, book)
        log.info("re-chunked %s (chunk rules %s -> %s)", path.parent.name, before, CHUNK_RULES_VERSION)
        return True
    except Exception:
        log.exception("re-chunking %s failed; serving its old chunks", path.parent.name)
        _upgrade_failed[key] = stamp
        return False
    finally:
        with _upgrade_lock:
            _upgrading.pop(key, None)


def schedule_all_upgrades() -> None:
    """Queue the upgrade of every out-of-date book (called at startup), whoever may see it."""
    for path in sorted(_books_dir().glob("*/book.json")):
        try:
            load_book(path.parent.name)
        except Exception:  # an unreadable book.json is reported where it is served
            log.warning("could not load %s for re-chunking", path.parent.name)


def wait_for_upgrades(timeout: float | None = 60) -> None:
    """Block until every queued upgrade has finished (tests, scripts)."""
    while True:
        with _upgrade_lock:
            pending = list(_upgrading.values())
        if not pending:
            return
        if wait(pending, timeout=timeout).not_done:
            return


SERIES_TTL = 60.0  # seconds; the index is also dropped when the books dir changes or a book is saved
_series_index: tuple[float, tuple, list[tuple[str, str, int]]] | None = None  # (built_at, dir mtime, entries)


def _series_entries() -> list[tuple[str, str, int]]:
    """(book id, series, series_index) of every book in a series. Cached so a question does not glob+parse every book."""
    global _series_index
    try:
        stamp = (str(_books_dir()), _books_dir().stat().st_mtime_ns)
    except OSError:
        stamp = (str(_books_dir()), 0)
    now = time.monotonic()
    if _series_index and now - _series_index[0] < SERIES_TTL and _series_index[1] == stamp:
        return _series_index[2]
    entries = []
    for path in _books_dir().glob("*/book.json"):
        other = load_book(path.parent.name)
        if other and other.series and other.series_index is not None:
            entries.append((path.parent.name, other.series, other.series_index))
    _series_index = (now, stamp, entries)
    return entries


def earlier_in_series(book: Book) -> list[Book]:
    """Books of the same series that come before `book`, in series order (all already read by its reader)."""
    if not book.series or book.series_index is None:
        return []
    out = []
    for bid, series, idx in _series_entries():
        if series == book.series and idx < book.series_index:
            other = load_book(bid)
            if other:
                out.append(other)
    return sorted(out, key=lambda b: b.series_index)


def trim_to_sentence(text: str, max_words: int) -> str:
    """Cut `text` to at most max_words at the last sentence end, never mid-sentence.

    Falls back to a word cut with an ellipsis only if the first sentence alone is longer than the limit.
    """
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    head = " ".join(words[:max_words])
    ends = [m.end() for m in re.finditer(r"[.!?][\"'”’)]*(?=\s|$)", head)]
    if ends:
        return head[:ends[-1]].strip()
    return head + "…"


def series_recap(book: Book, max_words: int = 160, full: bool = False) -> str:
    """One closing summary per earlier book of the series ("previously"), each ending on a whole sentence
    within max_words (full=True keeps the whole summary)."""
    parts = []
    for b in earlier_in_series(book):
        if b.summaries:
            text = max(b.summaries, key=lambda s: s.upto_para).text
            parts.append(f"{b.title}: {text.strip() if full else trim_to_sentence(text, max_words)}")
    return "\n".join(parts)


def list_books(hidden: bool = False) -> list[dict]:
    """The caller's library; hidden=True lists only the built-in books the caller hid instead."""
    from . import users
    out = []
    hidden_ids = users.hidden_books()
    for path in sorted(_books_dir().glob("*/book.json")):
        if not users.can_see_book(path.parent.name) or (path.parent.name in hidden_ids) != hidden:
            continue
        book = load_book(path.parent.name)
        if book is None:
            continue
        out.append({
            "id": book.id, "title": book.title, "author": book.author, "kind": book.kind,
            "kind_detected": book.kind_detected.model_dump() if book.kind_detected else None,
            "license": book.license, "attribution": book.attribution,
            "paragraphs": len(book.paragraphs), "sections": len(book.sections),
            "characters": sum(1 for c in book.characters if c.main is not False), "cover": portrait_url(book.id, "cover"),
            "series": book.series, "series_index": book.series_index,
        })
    return sorted(out, key=lambda b: (b["series"] or "", b["series_index"] or 0, b["id"]))


def save_book(path, book: Book) -> None:
    """Write book.json atomically (temp file + rename) so a running server never reads a half-written book."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(path)
    global _series_index
    _series_index = None
