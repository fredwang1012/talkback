"""Read books produced by the preprocess pipeline from data/books/<id>/book.json."""
import re

from .config import settings
from .schema import Book

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
    return book


def earlier_in_series(book: Book) -> list[Book]:
    """Books of the same series that come before `book`, in series order (all already read by its reader)."""
    if not book.series or book.series_index is None:
        return []
    out = []
    for path in _books_dir().glob("*/book.json"):
        other = load_book(path.parent.name)
        if other and other.series == book.series and other.series_index is not None \
                and other.series_index < book.series_index:
            out.append(other)
    return sorted(out, key=lambda b: b.series_index)


def series_recap(book: Book, max_words: int = 120) -> str:
    """One closing summary per earlier book of the series ("previously"), each trimmed to max_words."""
    parts = []
    for b in earlier_in_series(book):
        if b.summaries:
            words = max(b.summaries, key=lambda s: s.upto_para).text.split()
            text = " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
            parts.append(f"{b.title}: {text}")
    return "\n".join(parts)


def list_books() -> list[dict]:
    out = []
    for path in sorted(_books_dir().glob("*/book.json")):
        book = load_book(path.parent.name)
        if book is None:
            continue
        out.append({
            "id": book.id, "title": book.title, "author": book.author, "kind": book.kind,
            "license": book.license, "attribution": book.attribution,
            "paragraphs": len(book.paragraphs), "sections": len(book.sections),
            "characters": len(book.characters), "cover": portrait_url(book.id, "cover"),
            "series": book.series, "series_index": book.series_index,
        })
    return sorted(out, key=lambda b: (b["series"] or "", b["series_index"] or 0, b["id"]))


def save_book(path, book: Book) -> None:
    """Write book.json atomically (temp file + rename) so a running server never reads a half-written book."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(path)
