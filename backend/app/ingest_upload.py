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
from .parsing import CODE_MARK, HEADING_MARK, parse
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
HEADING_REGEX = (r"^(?=.{1,69}\Z)(?:(?i:(?:chapter|part|book|section|unit)\s+(?:\d+|[ivxlc]+|" + _NUMBER_WORDS
                 + r")\b[^,]*(?<!-))"
                 r"|[IVXLC]+\.|\d{1,3}\s+[A-Z][^.!?]*|\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+[A-Z][^.!?]*"
                 r"|\x01.+)\Z")  # \x01: a heading found by font size alone (see parsing.HEADING_MARK)
_HEADING = re.compile(HEADING_REGEX)
_BARE_LABEL = re.compile(r"^(?i:chapter|part|book|section|unit)\s+(?:\d+|[ivxlc]+|" + _NUMBER_WORDS + r")\s*$", re.I)
_CONTENTS = re.compile(r"^(?:table of\s+)?contents$", re.I)
HEADING_BIG = 1.15     # a short line this much larger than the body text is a heading
HEADING_BOLD = 1.05    # ... or bold and slightly larger
TOC_RUN = 4            # this many headings in a row is a table of contents, not chapters

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


_MONO_FONT = re.compile(r"mono|courier|consolas|cmtt|typewriter|menlo|lucidacon|inconsolata|fixed|\bcour\b", re.I)


def _is_mono(spans: list[dict]) -> bool:
    """Whether every non-blank span of a line is set in a monospace font."""
    real = [s for s in spans if s["text"].strip()]
    return bool(real) and all(_MONO_FONT.search(s["font"]) for s in real)


def _font_info(spans: list[dict]) -> tuple[float, bool]:
    """(largest font size rounded to 0.5pt, whether all the text is bold) of a line's spans."""
    real = [s for s in spans if s["text"].strip()] or spans
    size = round(max((s["size"] for s in real), default=0.0) * 2) / 2
    bold = bool(real) and all(s["flags"] & 16 or "bold" in s["font"].lower() for s in real)
    return size, bold


_CAPTION = re.compile(r"^(?:figure|fig\.|table|box|exhibit)\s+\d+(?:[.-]\d+)*(?:\s*[:|–—]|\.(?=\s|$)|\s*$)", re.I)
_CAPTION_START = re.compile(r"^(?:figure|fig\.|table|box|exhibit)\s+\d+(?:[.-]\d+)*\b", re.I)
_CELL = re.compile(r"^[\d\s.,%+−±$()/:-]+$")
SMALL_FONT = 0.85      # text set below this fraction of the body size is a footnote or sidebar
TABLE_RUN = 3          # this many numeric-only lines in a row are table cells
COLUMN_GAP = 20.0      # points: a block must clear the page middle by this much to belong to one column


def _reading_order(blocks: list[list[tuple]], width: float) -> list[list[tuple]]:
    """Reorder blocks of a clearly two-column page: left column, then right, between full-width blocks."""
    mid = width / 2
    left = [b for b in blocks if max(ln[3] for ln in b) < mid + COLUMN_GAP / 2]
    right = [b for b in blocks if min(ln[1] for ln in b) > mid - COLUMN_GAP / 2]
    if len(left) < 2 or len(right) < 2 or len(left) + len(right) < len(blocks) * 0.8:
        return blocks
    out: list[list[tuple]] = []
    band_l: list[list[tuple]] = []
    band_r: list[list[tuple]] = []
    left_ids, right_ids = set(map(id, left)), set(map(id, right))

    def flush():
        out.extend(sorted(band_l, key=lambda b: b[0][2]))
        out.extend(sorted(band_r, key=lambda b: b[0][2]))
        band_l.clear()
        band_r.clear()

    for b in sorted(blocks, key=lambda b: b[0][2]):
        if id(b) in left_ids:
            band_l.append(b)
        elif id(b) in right_ids:
            band_r.append(b)
        else:  # spans both columns: a title, banner or wide figure caption
            flush()
            out.append(b)
    flush()
    return out


_LEADING_SYMBOLS = re.compile(r"^[^\w(]+")  # marker glyphs such as a leading triangle before "Figure 2.10"


def _is_caption(blk: list[tuple], body: float) -> bool:
    text = _LEADING_SYMBOLS.sub("", " ".join(ln[0] for ln in blk))
    if _CAPTION.match(text):
        return len(blk) <= 4
    # "Figure 8.2 The ..." without punctuation is a caption only when set small or bold, else a body sentence
    size = max(ln[5] for ln in blk)
    return bool(_CAPTION_START.match(text)) and len(blk) <= 3 and (
        size < body * 0.95 or all(ln[6] for ln in blk))


def _strip_small_captions(blocks: list[list[tuple]], body: float) -> list[list[tuple]]:
    """Remove a small-font caption that shares a block with body text ("◮Figure 2.10 ..." plus its wrapped lines).

    A caption starts at a line that begins with Figure/Table/... (after any marker symbol) and is set smaller
    than the body text; it runs over the following lines that are also set small and close below. The block is
    split around it so the text after the caption stays a block of its own.
    """
    out: list[list[tuple]] = []
    for blk in blocks:
        cur: list[tuple] = []
        i = 0
        while i < len(blk):
            ln = blk[i]
            if body and ln[5] <= body * 0.95 and _CAPTION_START.match(_LEADING_SYMBOLS.sub("", ln[0])):
                i += 1
                while i < len(blk) and blk[i][5] <= body * 0.95 and blk[i][2] - blk[i - 1][4] < 1.5 * (blk[i][4] - blk[i][2]):
                    i += 1
                if cur:
                    out.append(cur)
                    cur = []
                continue
            cur.append(ln)
            i += 1
        if cur:
            out.append(cur)
    return out


def _clean_layout(blocks: list[list[tuple]], body: float) -> list[list[tuple]]:
    """Drop captions, small-font text (footnotes, sidebars) and runs of numeric table cells."""
    blocks = _strip_small_captions(blocks, body)
    kept = [b for b in blocks
            if not _is_caption(b, body) and not (body and max(ln[5] for ln in b) < body * SMALL_FONT)]
    out: list[list[tuple]] = []
    i = 0

    def numeric(b):
        return all(_CELL.match(ln[0]) for ln in b)

    while i < len(kept):
        if numeric(kept[i]):
            j = i
            n_lines = 0
            while j < len(kept) and numeric(kept[j]):
                n_lines += len(kept[j])
                j += 1
            if n_lines >= TABLE_RUN:
                i = j
                continue
        out.append(kept[i])
        i += 1
    return out


def _has_dot_leader(text: str) -> bool:
    """A TOC line: dots (a leader) followed by a page number. Linear: only the line's tail is looked at."""
    if len(text) > 300:
        return False
    t = text.rstrip()
    digits = len(t) - len(t.rstrip("0123456789"))
    if not 1 <= digits <= 4:
        return False
    return t[:-digits].rstrip()[-12:].replace(" ", "").endswith("...")


def _drop_contents(blocks: list[list[tuple]]) -> list[list[tuple]]:
    """Drop table-of-contents furniture: a 'Contents' title and lines ending in dot leaders plus a page number."""
    out = []
    for blk in blocks:
        if len(blk) == 1 and _CONTENTS.match(blk[0][0]):
            continue
        blk = [ln for ln in blk if not _has_dot_leader(ln[0])]
        if blk:
            out.append(blk)
    return out


_BARE_INDEX = re.compile(r"^\d{1,3}$")
_NUMBERED = re.compile(r"^\d{1,3}\s+\S")
_CODE_SYMBOLS = re.compile("[←⇐≠]")  # assignment arrows and "not equal"; prose also uses angle brackets, <= and ->
_CODE_WORDS = re.compile(r"\b(?:if|then|else|elseif|while|do|for each|return|NIL|null|until|repeat|end)\b")
_CODE_TITLE = re.compile(r"^[A-Z][A-Z0-9_-]{3,}\(.*\)$")  # "INTERSECTWITHSKIPS(p1, p2)"
CODE_MIN_LINES = 3


def _code_score(blk: list[tuple]) -> bool:
    """Whether a block is program code or pseudocode: monospace, or numbered lines full of keywords / symbols."""
    lines = [ln[0] for ln in blk]
    if len(lines) < 2:
        return False
    mono = sum(len(ln[0]) for ln in blk if len(ln) > 7 and ln[7])
    if mono >= 0.8 * sum(len(t) for t in lines) and len(lines) >= 2:
        return True
    numbers = sum(1 for t in lines if _BARE_INDEX.match(t))
    others = [t for t in lines if not _BARE_INDEX.match(t)]
    if len(lines) < CODE_MIN_LINES or not others:
        return False
    arrows = sum(1 for t in others if _CODE_SYMBOLS.search(t))
    words = sum(1 for t in others if _CODE_SYMBOLS.search(t) or _CODE_WORDS.search(t))
    numbered = numbers + sum(1 for t in others if _NUMBERED.match(t))
    if arrows >= 2 and arrows >= 0.4 * len(others):
        return True
    return numbered >= 0.4 * len(lines) and numbered >= 3 and words >= 0.6 * len(others) and words >= 2


def _code_text(blocks: list[list[tuple]]) -> str:
    """The listing's lines, each bare line number joined to the code that follows it."""
    out: list[str] = []
    pending = ""
    for blk in blocks:
        for ln in blk:
            t = ln[0]
            if _BARE_INDEX.match(t):
                pending = (pending + " " + t).strip() if pending else t
                continue
            out.append((pending + " " + t).strip())
            pending = ""
    if pending:
        out.append(pending)
    return "\n".join(out)


def _mark_code(blocks: list[list[tuple]]) -> list[list[tuple]]:
    """Turn each run of code-like blocks (plus a leading 'NAME(args)' title block) into one CODE_MARK line.

    The line stands alone as a paragraph; parse() makes it a paragraph of kind "code" (shown, not read).
    """
    flags = [_code_score(b) for b in blocks]
    out: list[list[tuple]] = []
    i = 0
    while i < len(blocks):
        title = i + 1 < len(blocks) and not flags[i] and flags[i + 1] and len(blocks[i]) == 1             and _CODE_TITLE.match(blocks[i][0][0])
        if not (flags[i] or title):
            out.append(blocks[i])
            i += 1
            continue
        j = i + 1
        while j < len(blocks) and flags[j]:
            j += 1
        run = blocks[i:j]
        lns = [ln for b in run for ln in b]
        out.append([(CODE_MARK + _code_text(run), min(ln[1] for ln in lns), lns[0][2], max(ln[3] for ln in lns),
                     lns[-1][4], max(ln[5] for ln in lns), False, False)])
        i = j
    return out


def _split_by_size(blocks: list[list[tuple]]) -> list[list[tuple]]:
    """Start a new block wherever the font size jumps (a heading set tight against body text)."""
    out = []
    for blk in blocks:
        cur = [blk[0]]
        for ln in blk[1:]:
            if abs(ln[5] - cur[-1][5]) > 0.15 * max(ln[5], 1):
                out.append(cur)
                cur = []
            cur.append(ln)
        out.append(cur)
    return out


_FRONT_MATTER = re.compile(
    r"©|\(c\)|\bcopyright\b|\bisbn\b|all rights reserved|\buniversity press\b|\bprinted (?:in|on)\b|"
    r"\bpublished by\b|\bdraft of\b|\bonline edition\b", re.I)


def _heading_candidate(blk: list[tuple], body: float) -> tuple[str, bool] | None:
    """(text, found by font alone) when the block looks like a heading: the regex, or large / bold short text.

    Copyright, ISBN and publisher lines (front matter) are never headings, however large they are set.
    """
    if len(blk) > 2:
        return None
    text = " ".join(ln[0] for ln in blk)
    if _FRONT_MATTER.search(text) or text.startswith(CODE_MARK):
        return None
    if _is_heading(text):
        return text, False
    if len(text) > 68 or len(text) < 2 or not any(c.isalpha() for c in text) or re.search(r"[.!?,;:]$", text):
        return None
    size = max(ln[5] for ln in blk)
    if body and (size >= body * HEADING_BIG or (all(ln[6] for ln in blk) and size >= body * HEADING_BOLD)):
        return text, True
    return None


def _mark_headings(blocks: list[list[tuple]], body: float) -> list[list[tuple]]:
    """Return the page's blocks with each heading turned into one standalone line (flag at index 7).

    A run of TOC_RUN or more heading blocks in a row (or a block of that many heading lines) is a table of
    contents and is dropped. A bare "CHAPTER 8" / "Unit 2" is joined with the heading block right after it.
    """
    blocks = _split_by_size(blocks)
    cands = [_heading_candidate(b, body) for b in blocks]
    for i, b in enumerate(blocks):  # one block of many heading-like lines
        if len(b) >= TOC_RUN and all(_is_heading(ln[0]) for ln in b):
            cands[i] = ("", False)
    drop: set[int] = set()
    i = 0
    while i < len(blocks):
        if cands[i] is None:
            i += 1
            continue
        j = i
        while j < len(blocks) and cands[j] is not None:
            j += 1
        if j - i >= TOC_RUN or any(cands[k] == ("", False) for k in range(i, j)):
            drop.update(range(i, j))
        i = j
    out: list[list[tuple]] = []
    i = 0
    while i < len(blocks):
        blk, cand = blocks[i], cands[i]
        i += 1
        if i - 1 in drop:
            continue
        if cand is None:
            out.append([ln[:7] + (ln[0].startswith(CODE_MARK),) for ln in blk])  # a listing stands alone
            continue
        text, by_font = cand
        nxt = cands[i] if i < len(blocks) and i not in drop else None
        if _BARE_LABEL.match(text) and nxt is not None and not _BARE_LABEL.match(nxt[0]):
            text, by_font = text + " " + nxt[0], by_font or nxt[1]
            blk = blk + blocks[i]
            i += 1
        while by_font and i < len(blocks) and i not in drop and cands[i] is not None and cands[i][1] \
                and abs(blocks[i][0][5] - blk[0][5]) < 0.5 and not _is_heading(cands[i][0]) \
                and blocks[i][0][2] - blk[-1][4] < 1.5 * max(blk[0][5], 1):
            text += " " + cands[i][0]  # a title set over several lines (title page): one heading
            blk = blk + blocks[i]
            i += 1
        first, last = blk[0], blk[-1]
        mark = HEADING_MARK if by_font and not _is_heading(text) else ""
        out.append([(mark + text, first[1], first[2], max(ln[3] for ln in blk), last[4], first[5], first[6], True)])
    return out


_TOC_NUMBER = re.compile(r"^\d+(?:\.\d+)*$")
_TOC_TRAILING = re.compile(r"\S\s+\d{1,4}$")
TOC_MIN_LINES = 10     # a page needs this many lines to be judged a table of contents
TOC_FRACTION = 0.5     # ... and this share of them bare numbers (section / page) or ending in a page number


def _is_toc_page(blocks: list[list[tuple]]) -> bool:
    """A contents page without dot leaders: most lines are bare section/page numbers or end in a page number."""
    lines = [ln[0] for blk in blocks for ln in blk]
    if len(lines) < TOC_MIN_LINES:
        return False
    tocish = sum(1 for t in lines if _TOC_NUMBER.match(t) or (_TOC_TRAILING.search(t) and len(t) <= 120))
    return tocish >= TOC_FRACTION * len(lines)


_LEAD_NUM = re.compile(r"^(?:\d+(?:\.\d+)*\s+)+")
_TRAIL_NUM = re.compile(r"(?:\s+\d+(?:\.\d+)*)+$")


def _header_core(blk: list[tuple], body: float) -> str | None:
    """The title text of a block shaped like a running header ('2.4 Title 39', '36 2 Title'), else None.

    Page-number-first headers ('39 Title') count only when set smaller than the body text, so that a real
    chapter heading such as '12 The Wedding' is not mistaken for one.
    """
    text = " ".join(ln[0] for ln in blk)
    if len(blk) > 3 or len(text) > 100 or _BARE_LABEL.match(text):
        return None
    trailing = bool(_TRAIL_NUM.search(text)) and bool(re.search(r"[A-Za-z]\s+\d{1,4}$", text))
    leading2 = bool(re.match(r"^\d{1,4}\s+\d{1,2}\s+\S", text))
    leading = bool(_LEAD_NUM.match(text)) and body and max(ln[5] for ln in blk) < body * 0.95
    if not (trailing or leading2 or leading):
        return None
    core = _TRAIL_NUM.sub("", _LEAD_NUM.sub("", text)).strip()
    if core.lower() in ("chapter", "part", "book", "section", "unit", "page"):
        return None
    return core.lower() if sum(c.isalpha() for c in core) >= 3 else None


def _drop_running_headers(pages: list[list[list[tuple]]], body: float) -> list[list[list[tuple]]]:
    """Drop per-page running headers/footers made of a section title and a page number (changing every page).

    A page-edge block counts when its title (numbers stripped) repeats at the edge of another page or equals
    the title of a heading elsewhere ('2.4 Positional postings 39' above the real '2.4 Positional postings').
    """
    edge_cores: Counter = Counter()
    for blocks in pages:
        seen = set()
        for blk in ((blocks[0], blocks[-1]) if blocks else ()):
            core = _header_core(blk, body)
            if core:
                seen.add(core)
        edge_cores.update(seen)
    body_cores = set()
    for blocks in pages:
        for blk in blocks[1:-1]:
            if len(blk) <= 3:
                text = " ".join(ln[0] for ln in blk)
                if len(text) <= 100:
                    body_cores.add(_TRAIL_NUM.sub("", _LEAD_NUM.sub("", text)).strip().lower())

    def is_header(blk) -> bool:
        core = _header_core(blk, body)
        return bool(core) and (edge_cores[core] >= 2 or core in body_cores)

    out = []
    for blocks in pages:
        if blocks and is_header(blocks[0]):
            blocks = blocks[1:]
        if blocks and is_header(blocks[-1]):
            blocks = blocks[:-1]
        out.append(blocks)
    return out


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
        widths: list[float] = []
        total = 0
        sizes: Counter = Counter()  # font size -> characters, to find the body size
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
                        size, bold = _font_info(ln["spans"])
                        sizes[size] += len(t)
                        lines.append((t, x0, y0, x1, y1, size, bold, _is_mono(ln["spans"])))
                        total += len(t) + 1
                        if total > MAX_CHARS:  # checked per line so extraction stops as soon as the cap is crossed
                            raise UploadError("The document has too much text (5 million characters maximum).")
                if lines:
                    blocks.append(lines)
            pages.append(blocks)
            widths.append(page.rect.width)

    def key(block):
        return _norm_key(" ".join(ln[0] for ln in block))

    edge = Counter()
    for blocks in pages:
        for k in ({key(blocks[0]), key(blocks[-1])} if blocks else ()):
            edge[k] += 1
    repeated = {k for k, n in edge.items() if n >= 3 and n >= 0.2 * len(pages)}

    def is_noise(block) -> bool:
        return key(block) in repeated or bool(_PAGE_NUMBER.match(" ".join(ln[0] for ln in block)))

    body = sizes.most_common(1)[0][0] if sizes else 0.0

    def strip_noise(blocks):
        while blocks and is_noise(blocks[0]):
            blocks = blocks[1:]
        while blocks and is_noise(blocks[-1]):
            blocks = blocks[:-1]
        return blocks

    pages = [strip_noise(blocks) for blocks in pages]
    pages = _drop_running_headers(pages, body)
    all_lines: list[Line] = []
    for pno, blocks in enumerate(pages):
        if _is_toc_page(blocks):
            continue
        blocks = _mark_code(_drop_contents(_clean_layout(_reading_order(blocks, widths[pno]), body)))
        flat = [ln for blk in blocks for ln in blk]
        if not flat:
            continue
        x0s = sorted(ln[1] for ln in flat)
        x1s = sorted(ln[3] for ln in flat)
        left, right = x0s[len(x0s) // 5], x1s[len(x1s) * 9 // 10]
        for blk in _mark_headings(blocks, body):
            alone = len(blk) == 1 and blk[0][7]
            for t, x0, y0, x1, y1, _size, _bold, _h in blk:
                all_lines.append(Line(t, x0, x1, y0, y1, left, right, pno, alone))

    paras = merge_lines(all_lines)
    paras = [p for i, p in enumerate(paras) if i == 0 or p != paras[i - 1] or not p.startswith(HEADING_MARK)]
    text = "\n\n".join(paras)
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
            out.append(Paragraph(idx=len(out), section_id=p.section_id, text=piece, kind=p.kind))
    for s in sections:
        s.start_para = new_start.get(s.start_para, len(out))
    return out


_NUMBERED = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})(?![\d.]*\w)")
MAX_LEVEL = 4


def section_level(title: str) -> int:
    """Nesting depth from the heading's number: "2" or "Chapter 2" is 1, "2.1" is 2, "2.1.1" is 3 (capped)."""
    m = _NUMBERED.match(title.strip())
    return min(m.group(1).count(".") + 1, MAX_LEVEL) if m else 1


def build_book(book_id: str, title: str, author: str, kind: str, raw: str, kind_detected: dict | None = None) -> Book:
    sections, paragraphs = parse(raw, None, None, HEADING_REGEX)
    paragraphs = _cap_paragraphs(sections, paragraphs)
    if kind != "textbook":  # only textbooks skip listings; elsewhere a monospace block is just text to read
        for p in paragraphs:
            p.kind = None
    if kind == "textbook":  # novels keep a flat chapter list
        for sec in sections:
            sec.level = section_level(sec.title)
    if not paragraphs:
        raise UploadError("No paragraphs were found in the file.")
    book = Book(id=book_id, title=title, author=author, kind=kind, kind_detected=kind_detected, license=LICENSE,
                sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE))
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
    return book


def process_upload(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str,
                   raw: str | None = None, kind_detected: dict | None = None):
    """CPU-bound part of ingest (run in a thread): returns (book, retrieval chunks).

    Writes the retrieval chunks but NOT book.json: call `publish_book` once everything else (indexing) succeeded,
    so a half-processed upload is never listed or readable.
    """
    book = build_book(book_id, title, author, kind, raw if raw is not None else extract_text(data, ext), kind_detected)
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


KIND_KEYS = ("kind", "kind_detected")


def write_status(book_id: str, status: str, message: str | None = None, enrich: dict | None = None,
                 **kind_info) -> dict:
    """status.json: {status, message?, enrich?, kind?, kind_detected?}.

    `enrich` is the novel enrichment sub-status (see enrich/pipeline.py). `kind`/`kind_detected` are carried over from
    the previous status unless given.
    """
    prev = read_status(book_id) or {}
    out = {"status": status} | ({"message": message} if message else {}) | ({"enrich": enrich} if enrich else {})
    for k in KIND_KEYS:
        v = kind_info[k] if k in kind_info else prev.get(k)
        if v is not None:
            out[k] = v
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
