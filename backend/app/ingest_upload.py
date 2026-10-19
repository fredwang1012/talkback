"""Turn an uploaded PDF or .txt into a narrator-only book: extract text, parse, add TTS chunks, write retrieval chunks.

PDF extraction heuristics (deliberately simple):
- text is read per page as blocks (roughly paragraphs); lines inside a block are joined with spaces;
- a line ending in a hyphen right after a letter and followed by a lowercase letter is de-hyphenated ("exam-" + "ple");
- inside a block, a line that ends a sentence and is clearly shorter than the block's longest line starts a new paragraph;
- the first and last block of each page are dropped when the same text (digits ignored) repeats on many pages
  (running headers and footers), or when they are a bare page number;
- a paragraph that does not end a sentence at the bottom of a page is joined to the first paragraph of the next page.
"""
import json
import re
import secrets
import unicodedata
from collections import Counter
from pathlib import Path

from . import retrieval
from .config import settings
from .parsing import parse
from .schema import Book, Narrator
from .ttschunks import chunk_paragraph

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
NARRATOR_VOICE = "marcus"
LICENSE = "Uploaded by the user"
# a short block that starts "Chapter 3", "PART II", ... becomes a section heading
HEADING_REGEX = r"(?i)^(?:chapter|part|book|section)\s+(?:\d+|[ivxlc]+|[a-z]+)\b[^\n]{0,60}$"

_PAGE_NUMBER = re.compile(r"^\W*(?:page\s+)?(?:\d{1,5}|[ivxlc]{1,7})\W*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"""[.!?]["'”’)\]]*$""")


class UploadError(Exception):
    """A problem with the uploaded file that the user can act on."""


def make_book_id(title: str) -> str:
    slug = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:40].strip("-") or "book"
    return f"u-{slug}-{secrets.token_hex(2)}"


def _join_lines(lines: list[str]) -> list[str]:
    """Join soft-wrapped lines of one block into paragraphs (see module docstring)."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return []
    longest = max(len(ln) for ln in lines)
    paras: list[str] = []
    cur = ""
    for i, ln in enumerate(lines):
        if cur.endswith("-") and len(cur) > 1 and cur[-2].isalpha() and ln[:1].islower():
            cur = cur[:-1] + ln
        else:
            cur = f"{cur} {ln}" if cur else ln
        last = i == len(lines) - 1
        if not last and longest >= 40 and len(ln) < 0.7 * longest and _SENTENCE_END.search(ln):
            paras.append(cur)
            cur = ""
    if cur:
        paras.append(cur)
    return paras


def _norm_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def pdf_to_text(data: bytes) -> str:
    """Extract paragraph-separated text from PDF bytes. Raises UploadError if the PDF is unreadable or has no text."""
    import pymupdf as fitz
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise UploadError("The PDF could not be read.") from e
    with doc:
        pages: list[list[str]] = []  # per page: block texts (lines joined by newline)
        for page in doc:
            blocks = [b[4].strip() for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
            pages.append(blocks)
    # running headers/footers: first/last block text (digits ignored) repeated on many pages
    edge = Counter()
    for blocks in pages:
        for b in {_norm_key(blocks[0]), _norm_key(blocks[-1])} if blocks else ():
            edge[b] += 1
    repeated = {k for k, n in edge.items() if n >= 3 and n >= 0.2 * len(pages)}

    def is_noise(block: str) -> bool:
        one_line = " ".join(block.split())
        return _norm_key(block) in repeated or bool(_PAGE_NUMBER.match(one_line))

    paras: list[str] = []
    carry = False  # previous page ended mid-sentence
    for blocks in pages:
        if blocks and is_noise(blocks[0]):
            blocks = blocks[1:]
        if blocks and is_noise(blocks[-1]):
            blocks = blocks[:-1]
        first = True
        for block in blocks:
            for para in _join_lines(block.split("\n")):
                if first and carry and paras:
                    paras[-1] = f"{paras[-1]} {para}"
                else:
                    paras.append(para)
                first = False
                carry = False
        if paras:
            carry = not _SENTENCE_END.search(paras[-1])
    text = "\n\n".join(paras)
    if not text.strip():
        raise UploadError("No extractable text was found. Scanned (image-only) PDFs are not supported.")
    return text


def extract_text(data: bytes, ext: str) -> str:
    if ext == ".pdf":
        return pdf_to_text(data)
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise UploadError("The text file is empty.")
    return text


def build_book(book_id: str, title: str, author: str, kind: str, raw: str) -> Book:
    sections, paragraphs = parse(raw, None, None, HEADING_REGEX)
    if not paragraphs:
        raise UploadError("No paragraphs were found in the file.")
    book = Book(id=book_id, title=title, author=author, kind=kind, license=LICENSE,
                sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE))
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
    return book


def process_upload(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str):
    """CPU-bound part of ingest (run in a thread): returns (book, retrieval chunks) after writing book.json and chunks."""
    book = build_book(book_id, title, author, kind, extract_text(data, ext))
    book_dir = settings.data_dir / "books" / book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "book.json").write_text(book.model_dump_json(indent=1), encoding="utf-8")
    chunks = retrieval.make_chunks(book)
    retrieval.save_index(book_dir / "index", chunks, None)
    return book, chunks


def write_status(book_id: str, status: str, message: str | None = None) -> dict:
    out = {"status": status} | ({"message": message} if message else {})
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
