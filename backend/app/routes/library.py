"""The library: listing, hide / restore, the book itself, its figures and the recap."""
import gzip
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .. import books, deps, ingest_upload, recap, retrieval, users
from ..config import settings
from ..schema import Book


router = APIRouter()


@router.get("/api/books")
def get_books():
    return books.list_books()


@router.get("/api/hidden-books")
def get_hidden_books():
    """Built-in books the caller hid from the library (restorable)."""
    return [{"id": b["id"], "title": b["title"], "author": b["author"]} for b in books.list_books(hidden=True)]


def _builtin_for_account(book_id: str) -> None:
    """Hide/restore: a visible built-in book, and (PER_USER) a signed-in account so nobody hides it for everyone."""
    if not books._ID.match(book_id):
        raise HTTPException(404, "Book not found")
    users.guard_book(book_id)
    users.require_account()
    if book_id.startswith("u-"):
        raise HTTPException(403, "Books you uploaded are deleted, not hidden.")
    if not (settings.data_dir / "books" / book_id / "book.json").is_file():
        raise HTTPException(404, "Book not found")


@router.post("/api/books/{book_id}/hide", dependencies=[Depends(deps.require_access)])
def hide_book(book_id: str):
    """Remove a built-in book (classics, Harry Potter) from this account's library. Its files are never touched:
    they are shared, and their processing was expensive."""
    _builtin_for_account(book_id)
    users.set_hidden(book_id, True)
    return {"hidden": book_id}


@router.post("/api/books/{book_id}/restore", dependencies=[Depends(deps.require_access)])
def restore_book(book_id: str):
    _builtin_for_account(book_id)
    users.set_hidden(book_id, False)
    return {"restored": book_id}


_FIGURE_NAME = re.compile(r"^fig-\d{1,4}\.png$")


@router.get("/api/books/{book_id}/figures/{name}", dependencies=[Depends(deps.require_access)])
def get_figure(book_id: str, name: str):
    """A textbook figure (#40), gated like the book itself: the same access code and per-account visibility."""
    if not books._ID.match(book_id) or not _FIGURE_NAME.match(name):  # both come from the URL: no path walking
        raise HTTPException(404, "Figure not found")
    users.guard_book(book_id)
    path = ingest_upload.figures_dir(book_id) / name
    if not path.is_file():
        raise HTTPException(404, "Figure not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/books/{book_id}", response_model=Book, dependencies=[Depends(deps.require_access)])
def get_book(book_id: str, accept_encoding: str = Header(default="")):
    """The full book. Gated like the paid endpoints: a public deployment must not hand out a copyrighted text.
    Serialised and gzipped once per book version (a novel is several MB of JSON)."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    cached = deps._book_bodies.get(book_id)
    if cached is None or cached[0] is not book:  # load_book returns a new object when book.json changes
        body = book.model_dump_json().encode()
        cached = deps._book_bodies[book_id] = (book, body, gzip.compress(body, compresslevel=6))
    if "gzip" in accept_encoding.lower():
        return Response(cached[2], media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(cached[1], media_type="application/json")


@router.get("/api/books/{book_id}/recap", dependencies=[Depends(deps.require_access)])
def get_recap(book_id: str, para: int = 0, scope: recap.Scope | None = None,
              section: str | None = Query(None, max_length=80)):
    """Spoiler-safe "Previously..." for the reader's place: the latest stored summary written from text at or
    before `para` (never a later one), the chapter they are in, and the closing summaries of earlier books.
    With `scope` (chapter | book | series, see recap.py) just that recap: {scope, upto_para, chapter, text}; with
    `section` too, that chapter's recap (a textbook: any section; a novel: only as far as the reader has read)."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    _, para = retrieval._clamp(book, para)
    if scope is not None:
        return recap.scoped(book, para, scope, section)
    ok = [s for s in book.summaries if s.upto_para <= para]
    best = max(ok, key=lambda s: s.upto_para) if ok else None
    sections = [s for s in book.sections if s.start_para <= para]
    chapter = max(sections, key=lambda s: s.start_para).title if sections else ""
    return {"upto_para": best.upto_para if best else None, "chapter": chapter,
            "text": best.text if best else "", "earlier": books.series_recap(book),
            "earlier_full": books.series_recap(book, full=True)}
