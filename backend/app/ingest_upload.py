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
import hashlib
import bisect
import json
import re
import secrets
import shutil
import threading
import unicodedata
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from . import retrieval
from .config import settings
from .parsing import CODE_MARK, EQ_MARK, EQ_SEP, HEADING_MARK, inline_marker, parse, strip_inline
from .schema import Book, Narrator, Paragraph, Section
from .ttschunks import chunk_paragraph, rechunk_book, sentence_spans, split_long

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
_BARE_NUMBER = re.compile(r"^\d{1,3}$")  # a chapter number set alone in a huge font above its title
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
    marked: str | None = None  # `text` with inline formulas marked (see _mark_inline); the paragraph uses it


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")  # math fonts (CMEX10 brackets) extract as control codes such as \x01


def _clean_line_text(text: str) -> str:
    """A line's text without ligatures or control codes; "\x01" is the parser's heading mark and must never leak in."""
    return re.sub(r"\s{2,}", " ", _CONTROL.sub(" ", text.translate(_LIGATURES))).strip()


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
            parts[-1] = parts[-1][:-1] + (ln.marked or ln.text)  # de-hyphenate "exam-" + "ple"
        else:
            parts.append(ln.marked or ln.text)
        prev = ln
    if parts:
        paras.append(" ".join(parts))
    return paras


_MONO_FONT = re.compile(r"mono|courier|consolas|cmtt|typewriter|menlo|lucidacon|inconsolata|fixed|\bcour\b", re.I)


def _is_mono(spans: list[dict]) -> bool:
    """Whether every non-blank span of a line is set in a monospace font."""
    real = [s for s in spans if s["text"].strip()]
    return bool(real) and all(_MONO_FONT.search(s["font"]) for s in real)


# ---- display equations (#41) ----
# TeX math italic / symbols / big operators (CMMI, CMSY, CMEX and their AMS / Latin Modern / tx / px kin), OpenType
# math fonts (Cambria Math, STIX, XITS...) and the classic Symbol / MT Extra fonts of word processors.
_MATH_FONT = re.compile(
    r"^(?:[A-Z]{6}\+)?(?:CM(?:MI|SY|EX|BSY|MIB|BMI)\d|MSAM|MSBM|EU(?:FM|FB|SM|SB|EX)|RSFS|STIX|XITS|Asana|Euclid|"
    r"MathematicalPi|Symbol|MT-?Extra|(?:rtx|ntx|tx|px|zx)(?:mi|sy|ex|ia)|esint|stmary|wasy|.*math)", re.I)
_BIG_OP_FONT = re.compile(r"^(?:[A-Z]{6}\+)?(?:CMEX|EUEX|(?:rtx|ntx|tx|px)ex)", re.I)
_PROSE_WORD = re.compile(r"[A-Za-z\u00C0-\u024F]{3,}")
# operator names set upright in math; never counted as prose
_MATH_WORDS = {"log", "exp", "sin", "cos", "tan", "max", "min", "arg", "argmin", "argmax", "var", "cov", "cor", "det",
               "lim", "sup", "inf", "sign", "diag", "trace", "mod", "gcd", "sqrt", "rss", "tss", "mse", "rse", "logit"}
EQ_WEAK_CHARS = 24       # a prose-free line this short (a limit, an exponent, "(3.4)") may belong to an equation
EQ_ROW_GAP = 18.0        # points: a candidate this close beside a prose line on the same row is inline math
EQ_LINE_GAP = 0.6        # rows of one display equation are at most this many line heights apart
EQ_MAX_HEIGHT = 220.0    # points: a taller cluster is a page of formulas or a misread figure, not one equation
EQ_MAX_PIECES = 60       # lines: a bigger cluster is a plot drawn as text (markers, tick labels), not an equation
EQ_PAGE_LINES = 25_000   # pages with more lines than this are not searched (plots drawn as text reach ~19,000)
EQ_PAGE_MAX = 60         # equations per page
NOT_MATH, WEAK, STRONG = 0, 1, 2


def _math_class(spans: list[dict]) -> int:
    """STRONG: the line is an equation (at most two prose words, and a big operator, or math-font letters with an
    operator or at least three of them); WEAK: a short prose-free piece that may be part of one (a limit, an exponent,
    "SE(β0)", an equation number); else NOT_MATH. A table row such as "R2 0.612" is WEAK, never an equation alone."""
    math_alpha = big = symbols = prose_words = prose_letters = nonspace = 0
    has_op = False
    for s in spans:
        t = s["text"]
        if not t.strip():
            continue
        nonspace += sum(not c.isspace() for c in t)
        has_op = has_op or any(unicodedata.category(c) == "Sm" for c in t)
        font = s["font"]
        if _MATH_FONT.search(font):
            math_alpha += sum(c.isalpha() for c in t)
            if _BIG_OP_FONT.search(font):
                big += sum(not c.isspace() for c in t)
            continue
        symbols += sum(1 for c in t if (unicodedata.category(c) == "Sm" and c not in "-<>") or "\u0370" <= c <= "\u03ff")
        if _MONO_FONT.search(font):
            continue  # variable names set in a code font ("sales = β0 + β1 × TV")
        for w in _PROSE_WORD.findall(t):
            if w.lower() not in _MATH_WORDS:
                prose_words += 1
                prose_letters += len(w)
    if not nonspace:
        return NOT_MATH
    prose_free = prose_words <= 2 and prose_letters <= 0.4 * nonspace
    if prose_free and (big or (math_alpha and (has_op or math_alpha >= 3)) or (symbols >= 2 and prose_words == 0)):
        return STRONG
    return WEAK if prose_words == 0 and nonspace <= EQ_WEAK_CHARS else NOT_MATH


# ---- inline formulas ----
# Inside a prose line, a run of math-font spans (with the operators, digits and brackets between them) whose text
# would read wrong is marked for a picture and a Gemini reading: lost glyphs, accents, big operators, roots, or
# sub/superscripts (smaller or shifted spans). A plain italic x reads fine and is left as text.
_GLUE_WORD = re.compile(r"[A-Za-z\u00C0-\u024F]{2,}|\b(?:i\.e|e\.g|cf|etc|vs)\b")
_LEAD_CATEGORIES = {"Ps", "Sk", "Sm", "Lm", "Mn", "Zs"}   # brackets, accents, operators may open a formula


def _trail_punct_len(t: str) -> int:
    """Length of the trailing run of whitespace and , . ; : (a linear scan: a regex search here is quadratic on long runs)."""
    i = len(t)
    while i > 0 and (t[i - 1] in ",.;:" or t[i - 1].isspace()):
        i -= 1
    return len(t) - i


_STRUCTURE_CHARS = set("∑∫∬∭∮∏∐√∛∜")
INLINE_SMALL = 0.85     # a span this much smaller than the run's main size is a sub/superscript
INLINE_SHIFT = 0.2      # ... or one whose baseline is this many font sizes off
INLINE_MAX_CHARS = 120  # longer runs are not one inline formula


def _span_role(s: dict) -> str:
    """"m" a math-font span, "g" glue that may sit inside a formula (operators, digits, "log", "RSE"), "p" prose, " " blank.

    A short token with no lowercase letter ("SE", "RSE", "RSS") is glue: it is part of the formula it touches. A word
    with a lowercase letter ("where", "the") stays prose, so the formula does not swallow the sentence.
    """
    t = s["text"]
    if not t.strip():
        return " "
    if _MATH_FONT.search(s["font"]):
        return "m"
    for w in _GLUE_WORD.findall(t):
        if w.lower() in _MATH_WORDS:
            continue
        if len(w) > 3 or any(c.islower() for c in w):
            return "p"
    return "g"


def _main_line(real: list[dict]) -> tuple[float, float]:
    """(main font size, baseline) of a run: the lowest baseline at the main size, since an accent such as the hat
    of ˆβ is set on a raised baseline of its own."""
    main = max(s["size"] for s in real)
    return main, max(s["origin"][1] for s in real if s["size"] == main)


def _needs_picture(run: list[dict]) -> bool:
    real = [s for s in run if s["text"].strip()]
    text = "".join(s["text"] for s in real)
    if _CONTROL.search(text) or any(c in _STRUCTURE_CHARS or unicodedata.category(c) in ("Sk", "Lm", "Mn")
                                    for c in text):
        return True
    if any(_BIG_OP_FONT.search(s["font"]) for s in real):
        return True
    main, base = _main_line(real)
    return any(s["size"] < main * INLINE_SMALL or abs(s["origin"][1] - base) > main * INLINE_SHIFT for s in real)


def _span_ink(s: dict) -> tuple[float, float, float, float]:
    """A span's box. Big-operator fonts (sums, roots) report a box far taller than the glyph, so their box is the
    band around the baseline where the glyph actually sits."""
    x0, y0, x1, y1 = s["bbox"]
    if _BIG_OP_FONT.search(s["font"]):
        base, size = s["origin"][1], s["size"] or 10.0
        y0, y1 = base - 1.15 * size, base + 1.7 * size
    return x0, y0, x1, y1


def _run_bbox(run: list[dict], cut: int = 0) -> tuple[float, float, float, float]:
    """The run's box, its height kept near its main line so tall math-font boxes do not reach the next line.
    `cut` trailing characters of the last span (sentence punctuation) are left out, by their share of its width."""
    real = [s for s in run if s["text"].strip()]
    main, base = _main_line(real)
    inks = [_span_ink(s) for s in real]
    x0 = min(b[0] for b in inks)
    x1 = max(b[2] for b in inks)
    if cut:
        bx0, _, bx1, _ = run[-1]["bbox"]
        x1 = min(x1, bx1 - (bx1 - bx0) * cut / max(len(run[-1]["text"]), 1))
    y0 = max(min(b[1] for b in inks), base - 1.35 * main)
    y1 = min(max(b[3] for b in inks), base + 0.45 * main)
    return x0, y0, x1, y1


ROW_BASE = 0.45         # baselines this close (in font sizes) are one row that pymupdf split around a tall glyph
DECO_BASE = 1.15        # a root / sum / hat this close to a prose row, and sitting in its x-range, belongs to that row


def _line_baseline(spans: list[dict]) -> float:
    real = [s for s in spans if s["text"].strip()]
    return _main_line(real)[1] if real else 0.0


def _assemble_rows(block_lines: list[dict]) -> list[tuple[list[dict], float]]:
    """pymupdf lines of one block, regrouped into visual rows.

    A tall glyph (a root, a sum, a hat) is often its own line, and the words on either side of it are split into
    two more, so one sentence comes out as "RSE =", "√", "RSS/(n-2)". Lines whose baselines match are one row.
    A line with no prose word is then attached to the prose row it sits on, when it is close and overlaps that
    row's width. Pieces of a display equation stay separate: they are not attached to each other here.
    """
    items = []
    for ln in block_lines:
        if abs(ln["dir"][1]) > 0.2:
            continue
        spans = list(ln["spans"])
        if any(s["text"].strip() for s in spans):
            items.append((spans, _line_baseline(spans), _math_class(spans) == NOT_MATH))
    rows: list[list[dict]] = []
    bases: list[float] = []
    prose: list[bool] = []
    deco: list[tuple[float, list[dict]]] = []
    for spans, base, is_prose in sorted(items, key=lambda it: (not it[2], it[1])):
        if not is_prose:
            deco.append((base, spans))
            continue
        size = max((s["size"] for s in spans if s["text"].strip()), default=10)
        x0 = min(s["bbox"][0] for s in spans if s["text"].strip())
        x1 = max(s["bbox"][2] for s in spans if s["text"].strip())

        def near(i: int) -> bool:
            other = max(s["size"] for s in rows[i] if s["text"].strip())
            if min(size, other) < 0.8 * max(size, other):  # a margin note set smaller than the text
                return False
            ox0 = min(s["bbox"][0] for s in rows[i] if s["text"].strip())
            ox1 = max(s["bbox"][2] for s in rows[i] if s["text"].strip())
            return x0 - ox1 < 25 and ox0 - x1 < 25

        join = next((i for i, b in enumerate(bases)
                     if prose[i] and abs(base - b) <= ROW_BASE * max(size, 8) and near(i)), None)
        if join is None:
            rows.append(list(spans))
            bases.append(base)
            prose.append(True)
        else:
            rows[join].extend(spans)
    for base, spans in deco:
        base = _deco_anchor(spans, base)
        size = max(s["size"] for s in spans)
        x0, x1 = min(s["bbox"][0] for s in spans), max(s["bbox"][2] for s in spans)
        best, best_d = None, DECO_BASE * max(size, 8)
        for i, b in enumerate(bases):
            if not prose[i] or abs(base - b) > best_d:
                continue
            other = max(s["size"] for s in rows[i] if s["text"].strip())
            if min(size, other) < 0.8 * max(size, other):
                continue
            rx0 = min(s["bbox"][0] for s in rows[i] if s["text"].strip())
            rx1 = max(s["bbox"][2] for s in rows[i] if s["text"].strip())
            if x1 < rx0 - 20 or x0 > rx1 + 20:  # the gap a root occupies between "RSE =" and "RSS"
                continue
            best, best_d = i, abs(base - b)
        if best is None:
            rows.append(list(spans))
            bases.append(base)
            prose.append(False)
        else:
            rows[best].extend(spans)
    order = sorted(range(len(rows)), key=lambda i: bases[i])
    return [(rows[i], bases[i]) for i in order]


def _by_x(spans: list[dict]) -> list[dict]:
    return sorted(spans, key=lambda s: (round(s["bbox"][0]), 0 if _BIG_OP_FONT.search(s["font"]) else 1, s["bbox"][1]))


def _deco_anchor(spans: list[dict], base: float) -> float:
    """The row a math-only fragment belongs to. A root, sum or delimiter font reports its baseline near the top of a
    box far taller than the glyph, so the middle of the ink is where it actually sits."""
    real = [s for s in spans if s["text"].strip()]
    if real and all(_BIG_OP_FONT.search(s["font"]) for s in real):
        inks = [_span_ink(s) for s in real]
        return (min(b[1] for b in inks) + max(b[3] for b in inks)) / 2
    return base


def _attach_deco_rows(groups: list[list[list]]) -> None:
    """Move a math-only row onto the prose row it belongs to.

    pymupdf puts a root or a hat in its own block, so the row assembly inside one block never sees the words beside
    it. `groups` is per block a list of [spans, baseline], edited in place.
    """
    prose = [(bi, li) for bi, blk in enumerate(groups) for li, (spans, _b) in enumerate(blk)
             if _math_class(spans) == NOT_MATH]
    for bi, blk in enumerate(groups):
        for li, (spans, base) in enumerate(blk):
            if _math_class(spans) == NOT_MATH:
                continue
            real = [s for s in spans if s["text"].strip()]
            size = max(s["size"] for s in real)
            anchor = _deco_anchor(spans, base)
            x0, x1 = min(s["bbox"][0] for s in real), max(s["bbox"][2] for s in real)
            best, best_key = None, None
            limit = DECO_BASE * max(size, 8)
            for pb, pl in prose:
                pspans, pbase = groups[pb][pl]
                if abs(anchor - pbase) > limit:
                    continue
                other = max(s["size"] for s in pspans if s["text"].strip())
                if min(size, other) < 0.8 * max(size, other):
                    continue
                preal = [s for s in pspans if s["text"].strip()]
                rx0, rx1 = min(s["bbox"][0] for s in preal), max(s["bbox"][2] for s in preal)
                if x1 < rx0 - 20 or x0 > rx1 + 20:
                    continue
                # a root sits in the gap between "RSE =" and "RSS", not on top of the words of the line above
                covers = any(s["text"].strip() and _span_role(s) == "p" and x1 > s["bbox"][0] + 1
                             and x0 < s["bbox"][2] - 1 for s in pspans)
                key = (covers, abs(anchor - pbase))
                if best_key is None or key < best_key:
                    best, best_key = (pb, pl), key
            if best is not None:
                groups[best[0]][best[1]][0].extend(spans)
                blk[li] = None


def _opens_formula(text: str) -> bool:
    """Whether a glue span may start a formula: brackets, accents, operators, an operator name ("max"), or a short
    upright name with no lowercase letter ("SE", "RSE")."""
    t = text.strip()
    if t.lower() in _MATH_WORDS:
        return True
    letters = [c for c in t if c.isalpha()]
    if letters and len(letters) <= 4 and not any(c.islower() for c in letters):
        return True
    return all(unicodedata.category(c) in _LEAD_CATEGORIES for c in text)


def _inline_runs(spans: list[dict]) -> list[tuple[int, int, int]]:
    """(start, end, cut) span ranges of the line's inline formulas that need a picture (see above): spans[start:end]
    without the last `cut` characters (sentence punctuation after the formula)."""
    roles = [_span_role(s) for s in spans]
    runs: list[tuple[int, int, int]] = []
    i = 0
    while i < len(spans):
        if roles[i] == "p":
            i += 1
            continue
        j = i
        while j < len(spans) and roles[j] != "p":
            j += 1
        s, e = i, j
        while s < e and (roles[s] == " " or (roles[s] == "g" and not _opens_formula(spans[s]["text"]))):
            s += 1
        while e > s and roles[e - 1] == " ":
            e -= 1
        cut = 0
        while e > s and roles[e - 1] == "g":
            text = spans[e - 1]["text"]
            n = _trail_punct_len(text)
            if not n:
                break
            if n < len(text):
                cut = n
                break
            e -= 1
        run = spans[s:e]
        if "m" in roles[s:e] and len("".join(x["text"] for x in run)) <= INLINE_MAX_CHARS and _needs_picture(run):
            runs.append((s, e, cut))
        i = j
    return runs


def _mark_inline(spans: list[dict], page: int) -> str | None:
    """The line's cleaned text with each inline formula that needs a picture wrapped in a parsing.inline_marker,
    or None when there is none."""
    runs = _inline_runs(spans)
    if not runs:
        return None
    pieces: list[str] = []
    rest = ""
    pos = 0
    for s, e, cut in runs:
        pieces.append(_clean_piece(rest + "".join(x["text"] for x in spans[pos:s])))
        raw = "".join(x["text"] for x in spans[s:e])
        raw, rest = (raw[:-cut], raw[-cut:]) if cut else (raw, "")
        inner = _clean_line_text(raw)
        pieces.append((" " if raw[:1].isspace() else "") + inline_marker(page, _run_bbox(spans[s:e], cut), inner)
                      + (" " if raw[-1:].isspace() else ""))
        pos = e
    pieces.append(_clean_piece(rest + "".join(x["text"] for x in spans[pos:])))
    return re.sub(r" {2,}", " ", "".join(pieces)).strip()


def _clean_piece(text: str) -> str:
    """Like _clean_line_text but keeping one space at either end, so pieces can be joined."""
    return re.sub(r"\s{2,}", " ", _CONTROL.sub(" ", text.translate(_LIGATURES)))


EQ_INDENT = 0.12        # a cluster starting this far (share of the text width) right of the margin is set off


def _mark_math(blocks: list[list[tuple]], info: list[list[tuple[int, bool]]], pno: int) -> list[list[tuple]]:
    """Turn each display equation of a page into one EQ_MARK line standing alone where its first piece was.

    `info[b][l]` is (class, lost glyphs) of line l of block b: the class from _math_class, and whether the raw text
    had control codes (glyphs of math fonts that extract as nothing) so the text is no faithful reading.

    A display equation is a cluster of STRONG / WEAK lines (with at least one STRONG) whose rows hold no prose: math
    beside prose on the same row is inline and stays in its sentence. Rows closer than EQ_LINE_GAP line heights join
    one cluster (a fraction's numerator and denominator, a sum's limits, a second line). The cluster must also be set
    off: indented / centred, taller than a text line (stacked rows, big delimiters), or with more than the page's
    normal leading to the prose above and below it, so the last line of a paragraph ("of β1.") is never taken for one.
    """
    if sum(map(len, blocks)) > EQ_PAGE_LINES:
        return blocks
    code = {bi for bi, blk in enumerate(blocks) if _code_score(blk)}  # pseudocode sets its arrows in math fonts too
    flat = [(bi, li, ln, NOT_MATH if bi in code else info[bi][li][0])
            for bi, blk in enumerate(blocks) for li, ln in enumerate(blk)]
    if not any(c == STRONG for *_, c in flat):
        return blocks
    prose = sorted((ln for _bi, _li, ln, c in flat if c == NOT_MATH), key=lambda p: p[2])
    prose_base = {id(ln): info[bi][li][2] for bi, blk in enumerate(blocks) for li, ln in enumerate(blk)
                  if bi not in code and info[bi][li][0] == NOT_MATH}
    def beside_prose(ln: tuple, base: float, share: float) -> bool:
        """Same row as prose when the baselines are close. The font box is not used: a sum or a root reports a box
        tall enough to overlap the line above and the line below."""
        band = (0.55 if share >= 0.4 else 0.9) * max(ln[5], 8.0)
        for p in prose:
            if abs(base - prose_base.get(id(p), (p[2] + p[4]) / 2)) <= band \
                    and max(ln[1], p[1]) - min(ln[3], p[3]) < EQ_ROW_GAP:
                return True
        return False

    # a WEAK piece (an inline sum's limit) touching a prose row belongs to that row; a STRONG line must share it more
    cands = sorted(((ln[2], bi, li) for bi, li, ln, c in flat
                    if c != NOT_MATH and not beside_prose(ln, info[bi][li][2], 0.4 if c == STRONG else 0.2)))
    clusters: list[list[tuple[int, int]]] = []
    bottom = 0.0
    for y0, bi, li in cands:
        ln = blocks[bi][li]
        # a delimiter's font box is far taller than the glyph; measuring the gap with that box glues the
        # equation above to the one below. Cap the height at the font size.
        span = min(ln[4] - ln[2], 1.3 * max(ln[5], 8.0))
        if clusters and y0 - bottom <= EQ_LINE_GAP * max(span, 1.0):
            clusters[-1].append((bi, li))
            bottom = max(bottom, ln[4])
        else:
            clusters.append([(bi, li)])
            bottom = ln[4]
    if prose:
        x0s, x1s = sorted(p[1] for p in prose), sorted(p[3] for p in prose)
        left, right = x0s[len(x0s) // 5], x1s[len(x1s) * 9 // 10]
        heights = sorted(p[4] - p[2] for p in prose)
        line_h = heights[len(heights) // 2]
    else:
        left = right = line_h = 0.0
    gaps = sorted(b[1][2] - b[0][4] for blk in blocks for b in zip(blk, blk[1:]) if b[1][2] >= b[0][4] - 1)
    leading = max(gaps[len(gaps) // 2], 0.0) if gaps else 0.0  # the page's normal space between lines of prose

    def set_off(lns: list[tuple]) -> bool:
        x0, y0, x1, y1 = min(l[1] for l in lns), min(l[2] for l in lns), max(l[3] for l in lns), max(l[4] for l in lns)
        if right > left and x0 - left >= EQ_INDENT * (right - left):
            return True
        if line_h and y1 - y0 > 1.5 * line_h:
            return True
        lead = leading + 0.2 * min(l[4] - l[2] for l in lns)
        above = [y0 - p[4] for p in prose if p[4] <= y0 + 1 and p[1] < x1 and x0 < p[3]]
        below = [p[2] - y1 for p in prose if p[2] >= y1 - 1 and p[1] < x1 and x0 < p[3]]
        return (not above or min(above) > lead) and (not below or min(below) > lead)

    member: dict[tuple[int, int], int] = {}
    for n, cl in enumerate(clusters):
        lns = [blocks[bi][li] for bi, li in cl]
        if len(cl) <= EQ_MAX_PIECES and any(info[bi][li][0] == STRONG and bi not in code for bi, li in cl) \
                and max(l[4] for l in lns) - min(l[2] for l in lns) <= EQ_MAX_HEIGHT and set_off(lns):
            member.update({k: n for k in cl})
    if not member:
        return blocks
    out: list[list[tuple]] = []
    done: set[int] = set()
    for bi, blk in enumerate(blocks):
        cur: list[tuple] = []
        for li, ln in enumerate(blk):
            n = member.get((bi, li))
            if n is None:
                cur.append(ln)
                continue
            if n in done:
                continue
            done.add(n)
            if cur:
                out.append(cur)
                cur = []
            if len(done) > EQ_PAGE_MAX:
                out.append([blocks[b][l] for b, l in clusters[n]])
                continue
            pieces = [blocks[b][l] for b, l in clusters[n]]
            out.append([_equation_line(pno, pieces, any(info[b][l][1] for b, l in clusters[n]),
                                       _equation_clip(pieces, [info[b][l][2] for b, l in clusters[n]], prose,
                                                      prose_base))])
        if cur:
            out.append(cur)
    return out


_EQ_NUMBER_TAIL = re.compile(r"\s*,?\s*\((\d{1,3}(?:\.\d{1,3}){0,2}[a-z]?)\)$")


def _equation_clip(lns: list[tuple], baselines: list[float], prose: list[tuple],
                   prose_base: dict) -> tuple[float, float, float, float]:
    """The cluster's box, stopped at the neighbouring prose line.

    A sum or a root box runs into the line above and the line below even though the glyph does not. The prose line's
    own edge is the limit, and only when its baseline is clearly off the equation's baseline.
    """
    x0, y0 = min(l[1] for l in lns), min(l[2] for l in lns)
    x1, y1 = max(l[3] for l in lns), max(l[4] for l in lns)
    base = sorted(baselines)[len(baselines) // 2]
    size = max(l[5] for l in lns) or 10.0
    for p in prose:
        pb = prose_base.get(id(p), (p[2] + p[4]) / 2)
        if pb > base + 0.8 * size:
            y1 = min(y1, pb - 0.85 * size)  # above the next line's letters, not its font box
        elif pb < base - 0.8 * size:
            y0 = max(y0, pb + 0.35 * size)
    if y1 < y0 + 1:  # a neighbour on every side: keep the cluster's own box
        return (min(l[1] for l in lns), min(l[2] for l in lns), max(l[3] for l in lns), max(l[4] for l in lns))
    return x0, y0, x1, y1


def _equation_line(pno: int, lns: list[tuple], lost_glyphs: bool,
                   clip: tuple[float, float, float, float] | None = None) -> tuple:
    """One EQ_MARK line for a cluster of equation lines (see parsing.EQ_MARK): its pieces row by row, left to right,
    and the equation number (wherever it sat) moved to the end."""
    x0, y0, x1, y1 = clip if clip is not None else (
        min(l[1] for l in lns), min(l[2] for l in lns), max(l[3] for l in lns), max(l[4] for l in lns))
    rows: list[list[tuple]] = []
    row_bottom = 0.0
    for l in sorted(lns, key=lambda l: (l[2] + l[4]) / 2):
        mid = (l[2] + l[4]) / 2
        if rows and mid <= row_bottom:
            rows[-1].append(l)
            row_bottom = max(row_bottom, l[4])
        else:
            rows.append([l])
            row_bottom = l[4]
    texts = [l[0] for row in rows for l in sorted(row, key=lambda l: l[1])]
    number = None
    tails = [(l[3], l[0]) for l in lns if _EQ_NUMBER_TAIL.search(l[0])]
    if tails:
        tail = max(tails)[1]
        number = _EQ_NUMBER_TAIL.search(tail).group(1)
        i = texts.index(tail)
        texts[i] = _EQ_NUMBER_TAIL.sub("", tail)
    content = [t for t in texts if t]
    text = " ".join(content) + (f" ({number})" if number else "")
    garbled = lost_glyphs or len(content) > 1 or any(unicodedata.category(c) in ("Sk", "Mn", "Lm") for c in text)
    head = f"{pno} {x0:.1f} {y0:.1f} {x1:.1f} {y1:.1f} {int(garbled)}"
    return (EQ_MARK + head + EQ_SEP + text, x0, y0, x1, y1, max(l[5] for l in lns), False, False)


def plain_text(raw: str) -> str:
    """pdf_to_text output without the equation and inline-formula markers (for samples shown to a model)."""
    return strip_inline(re.sub(EQ_MARK + "[^" + EQ_SEP + "]*" + EQ_SEP, "", raw))


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
        blk = [ln for ln in blk if ln[0].startswith(EQ_MARK) or not _has_dot_leader(ln[0])]
        if blk:
            out.append(blk)
    return out


_BARE_INDEX = re.compile(r"^\d{1,3}$")
_CODE_NUMBERED = re.compile(r"^\d{1,3}\s+\S")  # "12  x <- 0"; not "1. If ..." (a numbered prose list)
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
    numbered = numbers + sum(1 for t in others if _CODE_NUMBERED.match(t))
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


def _is_formula(text: str) -> bool:
    """A typeset equation or symbol line ("Score(q, d) = ∑"), never a heading."""
    return "=" in text or bool(re.search(r"\(\d+\.\d+\)$", text)) or any(unicodedata.category(c) in ("Sm", "So", "Co") and c not in "+-" for c in text)


_LEADING_DIGIT = re.compile(r"^\d")
FIGURE_MARGIN = 3.0     # points: a heading candidate this close to a labelled figure is part of the figure
FIGURE_LABELS = 3       # a drawing / image region with this many text lines on it is a figure with labels
FIGURE_MAX_AREA = 0.6   # a region covering more of the page than this is a page background, not a figure
THIN = 4.0              # points: an image or drawing thinner than this is a rule, not a figure
# Bounds on figure detection so a crafted PDF cannot spend unbounded CPU / memory on it (a page past any bound is
# simply treated as having no figures). The IR textbook's largest page content stream is ~250 KB.
MAX_CONTENT_BYTES = 1_000_000  # raw page content stream: above this, drawings are not even listed
MAX_DRAWINGS = 2000            # vector paths per page
MAX_FIGURE_RECTS = 200         # images + drawing clusters per page


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _figure_regions(rects: list[tuple], lines: list[tuple], page_area: float) -> list[tuple]:
    """The labelled figures of a page: drawing clusters / images with at least FIGURE_LABELS text lines on them.

    A banner image behind a lone chapter title has one line on it, so the title stays a heading.
    """
    if len(rects) > MAX_FIGURE_RECTS or not lines:
        return []
    by_top = sorted(lines, key=lambda ln: ln[2])
    tops = [ln[2] for ln in by_top]
    tallest = max(ln[4] - ln[2] for ln in by_top)
    out = []
    for x0, y0, x1, y1 in rects:
        if x1 - x0 < THIN or y1 - y0 < THIN or (x1 - x0) * (y1 - y0) > FIGURE_MAX_AREA * page_area:
            continue
        r = (x0 - FIGURE_MARGIN, y0 - FIGURE_MARGIN, x1 + FIGURE_MARGIN, y1 + FIGURE_MARGIN)
        n = 0  # only lines whose top lies within reach of the region are looked at
        for ln in by_top[bisect.bisect_left(tops, r[1] - tallest):bisect.bisect_right(tops, r[3])]:
            if _overlaps(r, ln[1:5]):
                n += 1
                if n >= FIGURE_LABELS:
                    out.append(r)
                    break
    return out


def _page_figures(doc, page, blocks: list[list[tuple]], rects: list[tuple]) -> list[tuple]:
    """Labelled figure regions of a page: its images (`rects`) plus its clustered vector drawings, within bounds."""
    if not blocks or len(rects) > MAX_FIGURE_RECTS:
        return []
    try:
        size = sum(len(doc.xref_stream_raw(x) or b"") for x in page.get_contents())
    except Exception:  # a broken content stream: no figure detection, the text is still read
        return []
    if size > MAX_CONTENT_BYTES:
        return []
    drawings = page.get_drawings() if size else []
    if len(drawings) > MAX_DRAWINGS:
        return []
    if drawings:
        rects = rects + [tuple(r) for r in page.cluster_drawings(drawings=drawings)]
    return _figure_regions(rects, [ln for blk in blocks for ln in blk], abs(page.rect))


def _in_figure(blk: list[tuple], figures: list[tuple]) -> bool:
    return any(_overlaps(f, ln[1:5]) for f in figures for ln in blk)


def _heading_candidate(blk: list[tuple], body: float) -> tuple[str, bool] | None:
    """(text, found by font alone) when the block looks like a heading: the regex, or large / bold short text.

    Copyright, ISBN and publisher lines (front matter) are never headings, however large they are set.
    """
    if len(blk) > 2:
        return None
    text = " ".join(ln[0] for ln in blk)
    if _FRONT_MATTER.search(text) or text.startswith((CODE_MARK, EQ_MARK)):
        return None
    size = max(ln[5] for ln in blk)
    if _is_heading(text):
        if body and size < body * 0.95 and _LEADING_DIGIT.match(text):
            return None  # "20 NEWSGROUPS" in a figure or sidebar: a numbered heading is never set below body size
        return text, False
    if _BARE_NUMBER.match(text):
        return (text, True) if body and size >= body * HEADING_BIG else None
    if _is_formula(text):
        return None
    if len(text) > 68 or len(text) < 2 or not any(c.isalpha() for c in text) or re.search(r"[.!?,;:]$", text):
        return None
    if body and (size >= body * HEADING_BIG or (all(ln[6] for ln in blk) and size >= body * HEADING_BOLD)):
        return text, True
    return None


def _mark_headings(blocks: list[list[tuple]], body: float, figures: list[tuple] = ()) -> list[list[tuple]]:
    """Return the page's blocks with each heading turned into one standalone line (flag at index 7).

    A run of TOC_RUN or more heading blocks in a row (or a block of that many heading lines) is a table of
    contents and is dropped. A bare "CHAPTER 8" / "Unit 2" is joined with the heading block right after it.
    Text on a labelled figure (`figures`, see _figure_regions) is never a heading.
    """
    blocks = _split_by_size(blocks)
    cands = [None if figures and _in_figure(b, figures) else _heading_candidate(b, body) for b in blocks]
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
            out.append([ln[:7] + (ln[0].startswith((CODE_MARK, EQ_MARK)),) for ln in blk])  # listings, equations stand alone
            continue
        text, by_font = cand
        nxt = cands[i] if i < len(blocks) and i not in drop else None
        if _BARE_NUMBER.match(text):  # "1" over "Boolean retrieval" is the heading "1 Boolean retrieval"
            if nxt is None or not nxt[1] or _BARE_NUMBER.match(nxt[0]):
                out.append([ln[:7] + (False,) for ln in blk])  # a lone big digit is not a heading
                continue
            text, by_font = text + " " + nxt[0], True
            blk = blk + blocks[i]
            i += 1
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
    tocish = sum(1 for t in lines if not t.startswith(EQ_MARK)
                 and (_TOC_NUMBER.match(t) or (_TOC_TRAILING.search(t) and len(t) <= 120)))
    return tocish >= TOC_FRACTION * len(lines)


_LEAD_NUM = re.compile(r"^(?:\d+(?:\.\d+)*\s+)+")
_TRAIL_NUM = re.compile(r"(?:\s+\d+(?:\.\d+)*)+$")


def _header_core(blk: list[tuple], body: float) -> tuple[str, bool] | None:
    """(title text, strong) of a block shaped like a running header ('2.4 Title 39', '36 2 Title'), else None.

    Page-number-first headers ('39 Title') are strong only when set smaller than the body text, so that a real
    chapter heading such as '12 The Wedding' is not mistaken for one. At body size and not bold they are weak:
    '484 Bibliography' counts only beside strong 'Bibliography 485' headers on facing pages.
    """
    text = " ".join(ln[0] for ln in blk)
    if len(blk) > 3 or len(text) > 100 or _BARE_LABEL.match(text) or text.startswith(EQ_MARK):
        return None
    size = max(ln[5] for ln in blk)
    trailing = bool(_TRAIL_NUM.search(text)) and bool(re.search(r"[A-Za-z]\s+\d{1,4}$", text))
    leading2 = bool(re.match(r"^\d{1,4}\s+\d{1,2}\s+\S", text))
    lead = bool(_LEAD_NUM.match(text)) and bool(body)
    leading = lead and size < body * 0.95
    weak = lead and size <= body * HEADING_BOLD and not all(ln[6] for ln in blk)
    if not (trailing or leading2 or leading or weak):
        return None
    core = _TRAIL_NUM.sub("", _LEAD_NUM.sub("", text)).strip()
    if core.lower() in ("chapter", "part", "book", "section", "unit", "page"):
        return None
    return (core.lower(), trailing or leading2 or leading) if sum(c.isalpha() for c in core) >= 3 else None


def _drop_running_headers(pages: list[list[list[tuple]]], body: float) -> list[list[list[tuple]]]:
    """Drop per-page running headers/footers made of a section title and a page number (changing every page).

    A page-edge block counts when its title (numbers stripped) repeats at the edge of another page or equals
    the title of a heading elsewhere ('2.4 Positional postings 39' above the real '2.4 Positional postings').
    """
    edge_cores: Counter = Counter()
    for blocks in pages:
        seen = set()
        for blk in ((blocks[0], blocks[-1]) if blocks else ()):
            hdr = _header_core(blk, body)
            if hdr and hdr[1]:
                seen.add(hdr[0])
        edge_cores.update(seen)
    body_cores = set()
    for blocks in pages:
        for blk in blocks[1:-1]:
            if len(blk) <= 3:
                text = " ".join(ln[0] for ln in blk)
                if len(text) <= 100:
                    body_cores.add(_TRAIL_NUM.sub("", _LEAD_NUM.sub("", text)).strip().lower())

    def is_header(blk) -> bool:
        hdr = _header_core(blk, body)
        if not hdr:
            return False
        core, strong = hdr
        return edge_cores[core] >= 2 or (strong and core in body_cores)

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
        figures: list[list[tuple]] = []  # page -> labelled figure regions (see _figure_regions)
        inline: dict[tuple, str] = {}  # (page, x0, y0, text) of a prose line -> its text with inline formulas marked
        for page in doc:
            blocks = []
            info = []  # per line: (math class, glyphs lost), see _mark_math
            rects = []  # images and drawing clusters: figure candidates
            raw_blocks = page.get_text("dict")["blocks"]
            grouped: list[list] = []
            for b in raw_blocks:
                if b.get("type") == 1:
                    rects.append(tuple(b["bbox"]))
                if b.get("type") != 0:
                    continue
                grouped.append([[spans, base] for spans, base in _assemble_rows(b["lines"])])
            _attach_deco_rows(grouped)
            for rows in grouped:
                lines, line_info = [], []
                for item in rows:
                    if item is None:
                        continue
                    spans, base = item
                    spans = _by_x(spans)
                    raw_text = "".join(s["text"] for s in spans)
                    t = _clean_line_text(raw_text)
                    if not t:
                        continue
                    x0 = min(s["bbox"][0] for s in spans)
                    y0 = min(s["bbox"][1] for s in spans)
                    x1 = max(s["bbox"][2] for s in spans)
                    y1 = max(s["bbox"][3] for s in spans)
                    size, bold = _font_info(spans)
                    sizes[size] += len(t)
                    lines.append((t, x0, y0, x1, y1, size, bold, _is_mono(spans)))
                    cls = _math_class(spans)
                    line_info.append((cls, bool(_CONTROL.search(raw_text)), base))
                    marked = _mark_inline(spans, page.number) if cls == NOT_MATH else None
                    if marked:
                        inline[(page.number, x0, y0, t)] = marked
                    total += len(t) + 1
                    if total > MAX_CHARS:  # checked per line so extraction stops as soon as the cap is crossed
                        raise UploadError("The document has too much text (5 million characters maximum).")
                if lines:
                    blocks.append(lines)
                    info.append(line_info)
            blocks = _mark_math(blocks, info, page.number)
            pages.append(blocks)
            widths.append(page.rect.width)
            figures.append(_page_figures(doc, page, blocks, rects))

    def key(block):
        return _norm_key(" ".join(ln[0] for ln in block))

    edge = Counter()
    for blocks in pages:
        for k in ({key(blocks[0]), key(blocks[-1])} if blocks else ()):
            edge[k] += 1
    repeated = {k for k, n in edge.items() if n >= 3 and n >= 0.2 * len(pages)}

    def is_noise(block) -> bool:
        text = " ".join(ln[0] for ln in block)
        if text.startswith(EQ_MARK):
            return False
        if _BARE_NUMBER.match(text) and body and max(ln[5] for ln in block) >= body * HEADING_BIG:
            return False  # a big lone number is a chapter number, not a page number
        return key(block) in repeated or bool(_PAGE_NUMBER.match(text))

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
        for blk in _mark_headings(blocks, body, figures[pno]):
            alone = len(blk) == 1 and blk[0][7]
            for t, x0, y0, x1, y1, _size, _bold, _h in blk:
                all_lines.append(Line(t, x0, x1, y0, y1, left, right, pno, alone, inline.get((pno, x0, y0, t))))

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
    return [text[s:e] for s, e in split_spans(text, limit)]


def split_spans(text: str, limit: int = MAX_PARAGRAPH_CHARS, keep: list[tuple[int, int]] = ()) -> list[tuple[int, int]]:
    """(start, end) pieces of split_paragraph; no cut falls inside a `keep` range (an inline formula)."""
    if len(text) <= limit:
        return [(0, len(text))]
    pieces: list[tuple[int, int]] = []
    for s, e in sentence_spans(text):
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


def _cap_paragraphs(sections: list[Section], paragraphs: list[Paragraph]) -> list[Paragraph]:
    """Re-index paragraphs after splitting over-long ones; section start_para values are remapped in place."""
    out: list[Paragraph] = []
    new_start: dict[int, int] = {}
    for p in paragraphs:
        new_start[p.idx] = len(out)
        keep = [(m.start, m.end) for m in p.inline_math]
        for s, e in ([(0, len(p.text))] if p.kind == "math" else split_spans(p.text, keep=keep)):
            inline = [m.model_copy(update={"start": m.start - s, "end": m.end - s})
                      for m in p.inline_math if s <= m.start and m.end <= e]
            out.append(Paragraph(idx=len(out), section_id=p.section_id, text=p.text[s:e], kind=p.kind, math=p.math,
                                 inline_math=inline))
    for s in sections:
        s.start_para = new_start.get(s.start_para, len(out))
    return out


_SECTION_NUMBER = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})(?![\d.]*\w)")
MAX_LEVEL = 4


def section_level(title: str) -> int:
    """Nesting depth from the heading's number: "2" or "Chapter 2" is 1, "2.1" is 2, "2.1.1" is 3 (capped)."""
    m = _SECTION_NUMBER.match(title.strip())
    return min(m.group(1).count(".") + 1, MAX_LEVEL) if m else 1


def build_book(book_id: str, title: str, author: str, kind: str, raw: str, kind_detected: dict | None = None) -> Book:
    sections, paragraphs = parse(raw, None, None, HEADING_REGEX)
    paragraphs = _cap_paragraphs(sections, paragraphs)
    if kind != "textbook":  # only textbooks skip listings and set equations; elsewhere they are just text to read
        for p in paragraphs:
            p.kind = p.math = None
            p.inline_math = []
    if kind == "textbook":  # novels keep a flat chapter list
        for sec in sections:
            sec.level = section_level(sec.title)
    if not paragraphs:
        raise UploadError("No paragraphs were found in the file.")
    book = Book(id=book_id, title=title, author=author, kind=kind, kind_detected=kind_detected, license=LICENSE,
                sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE))
    rechunk_book(book)  # stamps Book.chunk_rules
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


# ---- equation pictures (#41): cropped from the PDF after the book is published ----

EQ_DPI = 200
EQ_PAD = 2.0             # points around the equation's text box (fraction bars, accents, big delimiters)
EQ_MAX_PIXELS = 4_000_000
EQ_MAX_BOOK = 1500       # pictures per book


def equation_url(book_id: str, eq_id: str, png: bytes | None = None) -> str:
    """The picture's URL; `?v=` (from its bytes) makes a browser fetch a re-made crop despite its cache."""
    version = f"?v={hashlib.sha256(png).hexdigest()[:10]}" if png else ""
    return f"/api/books/{book_id}/equations/{eq_id}.png{version}"


def equation_path(book_id: str, eq_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "equations" / f"{eq_id}.png"


def _book_json(book_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "book.json"


def render_equations(book_id: str, data: bytes, stop=lambda: False) -> dict[str, tuple[bytes, str]]:
    """Worker thread: crop every equation of the published book from the PDF into equations/<id>.png and record the
    picture in book.json. Returns {equation id: (PNG, extracted text)} for the model. `stop()` is checked between
    pictures (a deleted book): nothing more is written, and the folder is never recreated."""
    import pymupdf
    path = _book_json(book_id)
    try:
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    eqs = [p for p in book.paragraphs if p.kind == "math" and p.math is not None][:EQ_MAX_BOOK]
    if not eqs:
        return {}
    out_dir = path.parent / "equations"
    try:
        out_dir.mkdir(exist_ok=True)  # never parents: a deleted book's folder must not come back
    except OSError:
        return {}
    found: dict[str, tuple[bytes, str]] = {}
    spans: dict = {}
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for p in eqs:
            m = p.math
            if stop():
                return {}
            png = _crop(doc, m.page, m.bbox, spans=spans)
            if png is None:
                continue
            equation_path(book_id, m.id).write_bytes(png)
            found[m.id] = (png, p.text)
    if stop():
        return {}
    if found:
        update_equations(book_id, {k: {"image": equation_url(book_id, k, png)} for k, (png, _t) in found.items()})
    return found


_PROSE_WORD = re.compile(r"[a-z]{2,}")   # "we", "known": a word, not a math italic y or an index i


def _crop(doc, page_no: int, bbox: list[float], pad: tuple[float, float] = (EQ_PAD, EQ_PAD),
          spans: dict | None = None, mask_prose: bool = False) -> bytes | None:
    """PNG of the box (padded by (x, y) points) on the page, or None when it is off the page, empty or too big.

    Text of other lines is painted out: math fonts report boxes far taller than their glyphs ("≈" reaches a line
    below its baseline), so the box can overlap the next line's letters. A span belongs to another line when its
    baseline lies outside the box; prose beside an inline formula is painted out too, up to the box's edge.
    With `mask_prose`, a word whose baseline sits inside a tall glyph's box (a sum or a root reaching the line
    above) is painted out as well. `spans` caches each page's span boxes across calls."""
    import pymupdf
    if not 0 <= page_no < doc.page_count:
        return None
    page = doc[page_no]
    x0, y0, x1, y1 = bbox
    clip = pymupdf.Rect(x0 - pad[0], y0 - pad[1], x1 + pad[0], y1 + pad[1]) & page.rect
    if clip.is_empty or clip.width * clip.height * (EQ_DPI / 72) ** 2 > EQ_MAX_PIXELS:
        return None
    pix = page.get_pixmap(clip=clip, dpi=EQ_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    if spans is None:
        spans = {}
    if page_no not in spans:
        spans[page_no] = [(pymupdf.Rect(s["bbox"]), s["origin"][1], s["size"], s["text"])
                          for b in page.get_text("dict")["blocks"]
                          for ln in b.get("lines", ()) for s in ln["spans"] if s["text"].strip()]
    anchor = main = None
    if mask_prose:
        body = [(base, size) for rect, base, size, text in spans[page_no]
                if y0 <= base <= y1 and rect.x1 > x0 and rect.x0 < x1 and not _PROSE_WORD.search(text)]
        if body:
            anchor = max(base for base, _size in body)
            main = max(size for _base, size in body)
    zoom = pymupdf.Matrix(EQ_DPI / 72, EQ_DPI / 72)
    for rect, base, size, text in spans[page_no]:
        if not rect.intersects(clip):
            continue
        area = rect & clip
        # a word on the line above, tucked into a tall sum or root, is not part of the formula
        tucked = (anchor is not None and _PROSE_WORD.search(text)
                  and (base < anchor - 0.7 * main or base > anchor + 0.45 * main))
        if base < y0 or base > y1 or tucked:   # another line: only where its letters are, not its font's whole box
            area.y0, area.y1 = max(area.y0, base - 0.78 * size), min(area.y1, base + 0.25 * size)
        else:   # the formula's own line: only prose beside it, outside the box, is painted out
            if rect.x0 >= x1 - 0.5:
                area.x0 = max(area.x0, x1)
            elif rect.x1 <= x0 + 0.5:
                area.x1 = min(area.x1, x0)
            else:
                continue
        if not area.is_empty:
            area = area * zoom
            pix.set_rect(pymupdf.IRect(area.x0, area.y0, area.x1 + 1, area.y1 + 1) & pix.irect, (255,))
    return pix.tobytes("png")


INLINE_MAX_BOOK = 3000   # distinct inline formulas cropped per book
INLINE_PAD = (1.0, 0.5)  # points (x, y): a taller pad reaches the descenders of the line above


def render_inline(book_id: str, data: bytes, stop=lambda: False) -> tuple[dict[str, tuple[bytes, str]],
                                                                          dict[str, list[str]]]:
    """Worker thread: crop the inline formulas of the published book, like render_equations. Formulas with the same
    extracted text and size share one picture (and one reading). Returns ({first id: (PNG, extracted text)},
    {first id: every id sharing it})."""
    import pymupdf
    path = _book_json(book_id)
    try:
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    groups: dict[tuple, list] = {}
    for p in book.paragraphs:
        for m in p.inline_math:
            x0, y0, x1, y1 = m.bbox
            key = (p.text[m.start:m.end], round(x1 - x0), round(y1 - y0))
            if key in groups:
                groups[key].append(m.id)
            elif len(groups) < INLINE_MAX_BOOK:
                groups[key] = [m, m.id]
    if not groups:
        return {}, {}
    out_dir = path.parent / "equations"
    try:
        out_dir.mkdir(exist_ok=True)  # never parents: a deleted book's folder must not come back
    except OSError:
        return {}, {}
    found: dict[str, tuple[bytes, str]] = {}
    shared: dict[str, list[str]] = {}
    spans: dict = {}
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for (text, _w, _h), (m, *ids) in groups.items():
            if stop():
                return {}, {}
            png = _crop(doc, m.page, m.bbox, INLINE_PAD, spans, mask_prose=True)
            if png is None:
                continue
            equation_path(book_id, m.id).write_bytes(png)
            found[m.id] = (png, text)
            shared[m.id] = ids
    if stop():
        return {}, {}
    if found:
        update_equations(book_id, {i: {"image": equation_url(book_id, k, found[k][0])}
                                   for k, ids in shared.items() for i in ids})
    return found, shared


_eq_lock = threading.Lock()   # read-modify-write of book.json by the equation job's threads


def update_equations(book_id: str, fields: dict[str, dict]) -> int:
    """Set MathBlock / InlineMath fields ({id: {image|latex|spoken: value}}) in book.json and re-chunk those
    paragraphs. Returns how many changed; nothing is written when book.json is gone (a deleted book)."""
    from . import books
    path = _book_json(book_id)
    with _eq_lock:
        try:
            book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        n = 0
        for p in book.paragraphs:
            changed = 0
            new = fields.get(p.math.id) if p.math is not None else None
            if new:
                p.math = p.math.model_copy(update=new)
                changed += 1
            for i, m in enumerate(p.inline_math):
                if new := fields.get(m.id):
                    p.inline_math[i] = m.model_copy(update=new)
                    changed += 1
            if not changed:
                continue
            p.tts_chunks = chunk_paragraph(p, book)
            n += changed
        if n:
            books.save_book(path, book)
    return n


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
    (d / "status.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def save_status(book_id: str, st: dict) -> bool:
    """Write a status snapshot as is; never recreates a book folder that is gone (a deleted book). True if written."""
    d = settings.data_dir / "books" / book_id
    if not d.is_dir():
        return False
    (d / "status.json").write_text(json.dumps(st), encoding="utf-8")
    return True


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


def delete_book_files(book_id: str, ident=None) -> list[str]:
    """Remove an uploaded book from disk: the book folder (book.json first, so it leaves the library at once), the
    original upload, avatar clips, portraits and the caller's cast file. Returns what could not be removed.

    Robust on a GCS FUSE mount, where removing a tree can fail part-way: each part is retried once and a failure never
    stops the others. Only the folders named after this exact book id are touched (the caller validates the id).
    """
    from . import users
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
    from . import users
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
    from . import books, users
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
