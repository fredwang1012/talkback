"""Turn an uploaded PDF or .txt into a narrator-only book: extract text, parse, add TTS chunks, write retrieval chunks.

PDF extraction heuristics (deliberately simple), working on text lines with their bounding boxes:
- the first and last block of each page are dropped when the same text (digits ignored) repeats on many pages
  (running headers and footers), or when they are a bare page number;
- consecutive lines (across blocks and pages) are merged into one paragraph, unless the previous line ends a
  sentence AND there is a paragraph signal: the next line is indented, there is a vertical gap larger than normal
  line spacing, or the previous line is clearly shorter than the page's text width (short last line);
- a block that is a single short heading line ("Chapter 3", "12 The Wedding", "CHAPTER ONE") stands on its own;
- a line ending in a hyphen right after a letter and followed by a lowercase letter is de-hyphenated ("exam-" + "ple").
Limits: pages, extracted characters and paragraph length are capped (see constants).
"""
import json
import re
import secrets
import shutil
import unicodedata
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from . import retrieval
from .config import settings
from .parsing import parse
from .schema import Book, Narrator, Paragraph, Section
from .ttschunks import chunk_paragraph, sentence_spans, split_long

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MULTIPART_SLACK = 1024 * 1024  # form-encoding overhead allowed on top of the file cap when checking Content-Length
MAX_PAGES = 2000
MAX_CHARS = 5_000_000
MAX_PARAGRAPH_CHARS = 5_000
NARRATOR_VOICE = "marcus"
LICENSE = "Uploaded by the user"
# A short (< 70 chars) block on its own becomes a section heading: "Chapter 3", "PART II", "CHAPTER ONE", "IV.",
# or "12 The Wedding" (number then capitalised title without sentence punctuation).
_NUMBER_WORDS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                 r"seventeen|eighteen|nineteen|twenty\S*|thirty\S*|forty\S*|fifty\S*|hundred")
HEADING_REGEX = (r"^(?=.{1,69}\Z)(?:(?i:(?:chapter|part|book|section)\s+(?:\d+|[ivxlc]+|" + _NUMBER_WORDS + r")\b[^,]*(?<!-))"
                 r"|[IVXLC]+\.|\d{1,3}\s+[A-Z][^.!?]*)\Z")
_HEADING = re.compile(HEADING_REGEX)

_PAGE_NUMBER = re.compile(r"^\W*(?:page\s+)?(?:\d{1,5}|[ivxlc]{1,7})\W*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"""[.!?]["'”’)\]]*$""")
INDENT_PT = 6.0       # a line starting this far right of the page's left margin is indented
GAP_FACTOR = 0.5      # a vertical gap above this fraction of the line height is a paragraph gap
SHORT_FRACTION = 0.75  # a sentence-ending line shorter than this fraction of the text width ends a paragraph


class UploadError(Exception):
    """A problem with the uploaded file that the user can act on."""


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


class Line(NamedTuple):
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    left: float    # page text margins (robust to indents and outliers)
    right: float
    page: int
    alone: bool    # this line is a block of its own that looks like a heading


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


def _norm_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def _is_heading(text: str) -> bool:
    return bool(_HEADING.match(text))


def _starts_paragraph(prev: Line, cur: Line) -> bool:
    if prev.alone or cur.alone:
        return True
    if not _SENTENCE_END.search(prev.text) or cur.text[:1].islower():
        return False  # mid-sentence, or a lowercase continuation such as '" said Ron.'
    if cur.x0 > cur.left + INDENT_PT:
        return True
    if cur.page == prev.page and cur.y0 - prev.y1 > GAP_FACTOR * max(prev.y1 - prev.y0, 1.0):
        return True
    return prev.x1 < prev.left + SHORT_FRACTION * (prev.right - prev.left)


def merge_lines(lines: list[Line]) -> list[str]:
    """Group consecutive lines into paragraphs (linear: each paragraph's parts are joined once)."""
    paras: list[str] = []
    parts: list[str] = []
    prev: Line | None = None
    for ln in lines:
        if prev is not None and _starts_paragraph(prev, ln):
            paras.append(" ".join(parts))
            parts = []
        if parts and parts[-1].endswith("-") and len(parts[-1]) > 1 and parts[-1][-2].isalpha() \
                and ln.text[:1].islower():
            parts[-1] = parts[-1][:-1] + ln.text  # de-hyphenate "exam-" + "ple"
        else:
            parts.append(ln.text)
        prev = ln
    if parts:
        paras.append(" ".join(parts))
    return paras


def pdf_to_text(data: bytes) -> str:
    """Extract paragraph-separated text from PDF bytes. Raises UploadError if the PDF is unreadable or has no text."""
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise UploadError("The PDF could not be read.") from e
    with doc:
        if doc.page_count > MAX_PAGES:
            raise UploadError(f"The PDF has too many pages ({MAX_PAGES} maximum).")
        pages: list[list[list[tuple]]] = []  # page -> blocks -> lines (text, x0, y0, x1, y1)
        total = 0
        for page in doc:
            blocks = []
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                lines = []
                for ln in b["lines"]:
                    t = "".join(s["text"] for s in ln["spans"]).translate(_LIGATURES).strip()
                    if t:
                        x0, y0, x1, y1 = ln["bbox"]
                        lines.append((t, x0, y0, x1, y1))
                        total += len(t) + 1
                        if total > MAX_CHARS:  # checked per line so extraction stops as soon as the cap is crossed
                            raise UploadError("The document has too much text (5 million characters maximum).")
                if lines:
                    blocks.append(lines)
            pages.append(blocks)

    def key(block):
        return _norm_key(" ".join(ln[0] for ln in block))

    edge = Counter()
    for blocks in pages:
        for k in ({key(blocks[0]), key(blocks[-1])} if blocks else ()):
            edge[k] += 1
    repeated = {k for k, n in edge.items() if n >= 3 and n >= 0.2 * len(pages)}

    def is_noise(block) -> bool:
        return key(block) in repeated or bool(_PAGE_NUMBER.match(" ".join(ln[0] for ln in block)))

    all_lines: list[Line] = []
    for pno, blocks in enumerate(pages):
        if blocks and is_noise(blocks[0]):
            blocks = blocks[1:]
        if blocks and is_noise(blocks[-1]):
            blocks = blocks[:-1]
        flat = [ln for blk in blocks for ln in blk]
        if not flat:
            continue
        x0s = sorted(ln[1] for ln in flat)
        x1s = sorted(ln[3] for ln in flat)
        left, right = x0s[len(x0s) // 5], x1s[len(x1s) * 9 // 10]
        for blk in blocks:
            alone = len(blk) == 1 and _is_heading(blk[0][0])
            for t, x0, y0, x1, y1 in blk:
                all_lines.append(Line(t, x0, x1, y0, y1, left, right, pno, alone))

    text = "\n\n".join(merge_lines(all_lines))
    if not text.strip():
        raise UploadError("No extractable text was found. Scanned (image-only) PDFs are not supported.")
    return text


def extract_text(data: bytes, ext: str) -> str:
    if ext == ".pdf":
        return pdf_to_text(data)
    text = data.decode("utf-8-sig", errors="replace")
    if len(text) > MAX_CHARS:
        raise UploadError("The document has too much text (5 million characters maximum).")
    if not text.strip():
        raise UploadError("The text file is empty.")
    return text


def split_paragraph(text: str, limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    """Split text longer than `limit` into pieces at sentence (else whitespace) boundaries."""
    if len(text) <= limit:
        return [text]
    pieces: list[tuple[int, int]] = []
    for s, e in sentence_spans(text):
        pieces += split_long(text[s:e], s, limit) if e - s > limit else [(s, e)]
    out: list[str] = []
    gs = ge = None
    for s, e in pieces:
        if gs is not None and e - gs <= limit:
            ge = e
        else:
            if gs is not None:
                out.append(text[gs:ge])
            gs, ge = s, e
    if gs is not None:
        out.append(text[gs:ge])
    return out


def _cap_paragraphs(sections: list[Section], paragraphs: list[Paragraph]) -> list[Paragraph]:
    """Re-index paragraphs after splitting over-long ones; section start_para values are remapped in place."""
    out: list[Paragraph] = []
    new_start: dict[int, int] = {}
    for p in paragraphs:
        new_start[p.idx] = len(out)
        for piece in split_paragraph(p.text):
            out.append(Paragraph(idx=len(out), section_id=p.section_id, text=piece))
    for s in sections:
        s.start_para = new_start.get(s.start_para, len(out))
    return out


def build_book(book_id: str, title: str, author: str, kind: str, raw: str) -> Book:
    sections, paragraphs = parse(raw, None, None, HEADING_REGEX)
    paragraphs = _cap_paragraphs(sections, paragraphs)
    if not paragraphs:
        raise UploadError("No paragraphs were found in the file.")
    book = Book(id=book_id, title=title, author=author, kind=kind, license=LICENSE,
                sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE))
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
    return book


def process_upload(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str):
    """CPU-bound part of ingest (run in a thread): returns (book, retrieval chunks).

    Writes the retrieval chunks but NOT book.json: call `publish_book` once everything else (indexing) succeeded,
    so a half-processed upload is never listed or readable.
    """
    book = build_book(book_id, title, author, kind, extract_text(data, ext))
    chunks = retrieval.make_chunks(book)
    retrieval.save_index(settings.data_dir / "books" / book_id / "index", chunks, None)
    return book, chunks


def publish_book(book: Book) -> None:
    """Write book.json (which makes the book visible to list_books/load_book)."""
    d = settings.data_dir / "books" / book.id
    tmp = d / "book.json.tmp"
    tmp.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(d / "book.json")


def discard_book(book_id: str) -> None:
    """Remove everything but status.json after a failed ingest."""
    d = settings.data_dir / "books" / book_id
    shutil.rmtree(d / "index", ignore_errors=True)
    for name in ("book.json", "book.json.tmp"):
        (d / name).unlink(missing_ok=True)


def write_status(book_id: str, status: str, message: str | None = None, enrich: dict | None = None) -> dict:
    """status.json: {status, message?, enrich?}. `enrich` is the novel enrichment sub-status (see enrich/pipeline.py)."""
    out = {"status": status} | ({"message": message} if message else {}) | ({"enrich": enrich} if enrich else {})
    d = settings.data_dir / "books" / book_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def read_status(book_id: str) -> dict | None:
    path = settings.data_dir / "books" / book_id / "status.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def upload_path(book_id: str, ext: str) -> Path:
    return settings.data_dir / "uploads" / f"{book_id}{ext}"
