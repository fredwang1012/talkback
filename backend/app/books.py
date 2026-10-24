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


def load_book(book_id: str) -> Book | None:
    if not _ID.match(book_id):  # ids come from the URL: never let them walk the filesystem
        return None
    path = _books_dir() / book_id / "book.json"
    if not path.is_file():
        return None
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    for c in book.characters:
        c.portrait = c.portrait or portrait_url(book_id, c.id)
    if book.narrator:
        book.narrator.portrait = book.narrator.portrait or portrait_url(book_id, "narrator")
    return book


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
        })
    return out
