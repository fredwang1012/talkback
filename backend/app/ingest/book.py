"""From extracted text to a Book: the paragraph cap, section levels, build_book and the upload's processing step."""
import re

from .. import booklang, books, retrieval
from ..config import settings
from ..parsing import parse
from ..schema import Book, Narrator, Paragraph, Section
from ..ttschunks import rechunk_book, sentence_spans, split_long
from .common import LICENSE, MAX_PARAGRAPH_CHARS, NARRATOR_VOICE, UploadError
from .dropcaps import _CHAPTER_TITLE, _PART
from .figures import figures_dir
from .headings import HEADING_REGEX
from .text import extract_text


def split_paragraph(text: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    """Split text longer than `limit` into pieces at sentence (else whitespace) boundaries."""
    return [text[s:e] for s, e in split_spans(text, limit)]


def split_spans(text: str, limit: int = MAX_PARAGRAPH_CHARS, keep: list[tuple[int, int]] = (),
                language: str | None = None) -> list[tuple[int, int]]:
    """(start, end) pieces of split_paragraph; no cut falls inside a `keep` range (an inline formula). `language`:
    the book's (its sentence rules)."""
    if len(text) <= limit:
        return [(0, len(text))]
    pieces: list[tuple[int, int]] = []
    for s, e in sentence_spans(text, language):
        pieces += split_long(text[s:e], s, limit) if e - s > limit else [(s, e)]
    out: list[tuple[int, int]] = []
    gs = ge = None
    for s, e in pieces:
        inside = any(a < s < b for a, b in keep)
        if gs is not None and (e - gs <= limit or inside):
            ge = e
        else:
            if gs is not None:
                out.append((gs, ge))
            gs, ge = s, e
    if gs is not None:
        out.append((gs, ge))
    return out


def _cap_paragraphs(sections: list[Section], paragraphs: list[Paragraph], language: str | None = None) -> list[Paragraph]:
    """Re-index paragraphs after splitting over-long ones; section start_para values are remapped in place."""
    out: list[Paragraph] = []
    new_start: dict[int, int] = {}
    for p in paragraphs:
        new_start[p.idx] = len(out)
        keep = [(m.start, m.end) for m in p.inline_math]
        for s, e in ([(0, len(p.text))] if p.kind == "math" else split_spans(p.text, keep=keep, language=language)):
            inline = [m.model_copy(update={"start": m.start - s, "end": m.end - s})
                      for m in p.inline_math if s <= m.start and m.end <= e]
            out.append(Paragraph(idx=len(out), section_id=p.section_id, text=p.text[s:e], kind=p.kind, math=p.math,
                                 inline_math=inline, figure=p.figure))
    for s in sections:
        s.start_para = new_start.get(s.start_para, len(out))
    return out


_SECTION_NUMBER = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})(?![\d.]*\w)")
MAX_LEVEL = 4


def section_level(title: str) -> int:
    """Nesting depth from the heading's number: "2" or "Chapter 2" is 1, "2.1" is 2, "2.1.1" is 3 (capped)."""
    m = _SECTION_NUMBER.match(title.strip())
    return min(m.group(1).count(".") + 1, MAX_LEVEL) if m else 1


def build_book(book_id: str, title: str, author: str, kind: str, raw: str, kind_detected: dict | None = None,
               language: str | None = None) -> Book:
    """The book of an upload's text. `language` (ISO 639-3) is detected from the text when not given (#117): it sets
    the sentence rules of the TTS chunks and is stored as Book.language."""
    sections, paragraphs = parse(raw, None, None, HEADING_REGEX)
    if language is None:
        language = booklang.detect("\n".join(p.text for p in paragraphs if p.kind is None))
    paragraphs = _cap_paragraphs(sections, paragraphs, language)
    if kind != "textbook":  # only textbooks skip listings, set equations and show figures; elsewhere they are just text
        for p in paragraphs:
            p.kind = p.math = p.figure = None
            p.inline_math = []
    if kind == "textbook":
        for sec in sections:
            sec.level = section_level(sec.title)
    else:  # novels keep a flat chapter list, except that the chapters of a part ("BOOK ONE") sit under it
        in_part = False
        for sec in sections:
            if _PART.match(sec.title):
                in_part = True
            elif in_part and _CHAPTER_TITLE.match(sec.title):
                sec.level = 2
            else:
                in_part = False
    if not paragraphs:
        raise UploadError("No paragraphs were found in the file.")
    book = Book(id=book_id, title=title, author=author, kind=kind, kind_detected=kind_detected, license=LICENSE,
                language=language, sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE))
    rechunk_book(book)  # stamps Book.chunk_rules
    return book


def rebuild_textbook_text(book: Book, data: bytes) -> Book:
    """Re-read the PDF of a book stored as a novel, as a textbook, so its equations and figures come back. Nothing is
    saved or indexed except the figure crops, written to the book's figures dir exactly as a textbook upload writes
    them (the caller applies the text with `apply_textbook_text` under the book's write lock).

    A paper the detector calls a novel is stored with every formula turned into plain text and no figure paragraphs.
    Moving it to Textbooks cannot recover the pictures from that text; the original file is read again.
    """
    detected = book.kind_detected.model_dump() if book.kind_detected else None
    raw = extract_text(data, ".pdf", figures_dir(book.id))
    return build_book(book.id, book.title, book.author, "textbook", raw, detected, book.language)


def apply_textbook_text(book: Book, rebuilt: Book) -> None:
    """Put the re-read text (`rebuild_textbook_text`) into `book` (a fresh read of book.json) and make it a textbook.
    Everything not derived from the text stays (characters, narrator, series, license, title...). Summaries do not:
    their paragraph numbers belong to the old text. The TTS chunks are rebuilt with the book's own voices."""
    book.kind = "textbook"
    book.sections = rebuilt.sections
    book.paragraphs = rebuilt.paragraphs
    book.summaries = []
    rechunk_book(book)  # stamps Book.chunk_rules


def process_upload(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str,
                   raw: str | None = None, kind_detected: dict | None = None):
    """CPU-bound part of ingest (run in a thread): returns (book, retrieval chunks).

    Writes the retrieval chunks but NOT book.json: call `publish_book` once everything else (indexing) succeeded,
    so a half-processed upload is never listed or readable.
    """
    if raw is None:
        raw = extract_text(data, ext, figures_dir(book_id))
    book = build_book(book_id, title, author, kind, raw, kind_detected)
    chunks = retrieval.make_chunks(book)
    retrieval.save_index(settings.data_dir / "books" / book_id / "index", chunks, None)
    return book, chunks


def publish_book(book: Book) -> None:
    """Write book.json (which makes the book visible to list_books/load_book)."""
    books.save_book(settings.data_dir / "books" / book.id / "book.json", book)
