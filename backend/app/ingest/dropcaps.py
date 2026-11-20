"""Drop caps: the big initial letter joined back onto its word, the epigraph above it kept, and a drop-cap
paragraph with no heading of its own counted as a chapter (see pdf.pdf_to_text)."""
import bisect
import re

from ..parsing import CODE_MARK, EQ_MARK, FIGURE_MARK, HEADING_MARK
from .headings import _is_heading


DROP_CAP = re.compile(r"^[\"'“‘«„]?[^\W\d_]$")  # an initial letter, maybe after an opening quote
DROP_RATIO = 1.8       # a drop cap is set at least this many times the size of the text it starts
DROP_MAX_CAPS = 50     # a page with more drop caps than this is not a novel page: they are left as they are
DROP_MAX_LINES = 5000  # ... and so is a page with more lines than this
DROP_CHAPTERS = 3      # a book with this many drop-cap starts (no heading over them) has them as its chapters
CHAPTER_MARK = "\x08"  # the line pdf_to_text puts where a drop-cap chapter starts; becomes "Chapter N"
_PART = re.compile(r"^\x01?(?:(?:book|part|libro|parte|livre|partie|teil|buch|deel|boek|księga|часть|книга)\s+\S"
                   r"|第[0-9０-９一二三四五六七八九十百千零〇两]+[部卷])", re.I)
_OPENING = re.compile(r"^\x01?(?:introduction|prologue|preface|foreword|introducción|prólogo|prefacio|préface|"
                      r"avant-propos|einleitung|vorwort|prolog|introduzione|prologo|prefazione)\b", re.I)  # restarts chapter numbers
_CHAPTER_TITLE = re.compile(r"^(?:(?:chapter|capítulo|capitulo|chapitre|kapitel|capitolo|hoofdstuk|rozdział|глава)\s+\S"
                            r"|第[0-9０-９一二三四五六七八九十百千零〇两]+[章回])", re.I)
_DASHES = ("—", "–", "―", "-")  # an epigraph's attribution starts with a dash ("—from ...")


def _merge_drop_caps(blocks: list[dict]) -> None:
    """Put each drop cap (a big initial letter, maybe after an opening quote, set beside the first lines of a
    paragraph) back at the start of its word, in place on pymupdf's raw page blocks.

    pymupdf gives the cap a line (often a block) of its own, so "Dune" came out as "D" + "une" and '"Y' was taken
    for a heading. The cap is joined to the topmost line that starts at its right edge within its height, and
    the lines beside it are moved to its left edge so they do not look indented. The word's span is flagged
    "dropcap": its paragraph starts there (see _keep_epigraphs and _mark_drop_chapters).

    Each cap only looks at the lines whose middles fall within its height (bisect over the lines sorted by
    middle); a page with more than DROP_MAX_CAPS caps or DROP_MAX_LINES lines is left alone (no real book page
    has them, and a crafted one must not make this slow).
    """
    items = []   # (middle y, line, its non-blank spans, its largest size, its leftmost span), sorted by middle
    caps = []    # (block, line, non-blank spans)
    for blk in blocks:
        if blk.get("type") != 0:
            continue
        for ln in blk["lines"]:
            if abs(ln["dir"][1]) > 0.2:
                continue
            real = [s for s in ln["spans"] if s["text"].strip()]
            if not real:
                continue
            if len(items) >= DROP_MAX_LINES:
                return
            mid = (min(s["bbox"][1] for s in real) + max(s["bbox"][3] for s in real)) / 2
            items.append((mid, ln, real, max(s["size"] for s in real), min(real, key=lambda s: s["bbox"][0])))
            if DROP_CAP.match("".join(s["text"] for s in real).strip()):
                caps.append((blk, ln, real))
    if not caps or len(caps) > DROP_MAX_CAPS:
        return
    items.sort(key=lambda it: it[0])
    mids = [it[0] for it in items]
    used: set[int] = set()
    for blk, cap, real in caps:
        if id(cap) in used:
            continue
        size = max(s["size"] for s in real)
        x0, x1 = min(s["bbox"][0] for s in real), max(s["bbox"][2] for s in real)
        y0, y1 = min(s["bbox"][1] for s in real), max(s["bbox"][3] for s in real)
        beside = []
        for mid, ln, _real, lsize, first in items[bisect.bisect_left(mids, y0):bisect.bisect_right(mids, y1)]:
            if ln is cap or id(ln) in used or lsize * DROP_RATIO > size:
                continue
            # a glyph box is often wider than its ink ("A" in a book face): the text may start inside it
            if (x0 + x1) / 2 < first["bbox"][0] <= x1 + max(20.0, 1.5 * lsize):
                beside.append((mid, id(ln), first))
        if not beside:
            continue
        first = beside[0][2]   # the items are sorted by middle: the first one is the topmost
        first["text"] = "".join(s["text"] for s in real).strip() + first["text"].lstrip()
        first["dropcap"] = True
        for _mid, lid, span in beside:
            span["bbox"] = (x0,) + tuple(span["bbox"][1:])
            used.add(lid)
        used.add(id(cap))
        blk["lines"] = [ln for ln in blk["lines"] if ln is not cap]


def _line_key(pno: int, ln: tuple) -> tuple:
    return pno, ln[1], ln[2], ln[0]


def _keep_epigraphs(blocks: list[list[tuple]], pno: int, drops: set, body: float) -> tuple[list[list[tuple]], set]:
    """(blocks, keys of epigraph lines). Each drop-cap line starts a block, and the text set smaller than it just
    above it on the page (a chapter's epigraph and its attribution) is brought to its size, so that it is kept as
    the chapter's opening text instead of being dropped as a footnote. (Never above the body size: an epigraph
    is no heading.)"""
    out: list[list[tuple]] = []
    for blk in blocks:
        cur: list[tuple] = []
        for ln in blk:
            if cur and _line_key(pno, ln) in drops:
                out.append(cur)
                cur = []
            cur.append(ln)
        out.append(cur)
    epigraph: set = set()
    for d, blk in enumerate(out):
        if _line_key(pno, blk[0]) not in drops:
            continue
        top, size = blk[0][2], blk[0][5]
        k = d - 1
        while k >= 0 and all(ln[4] <= top + 2 and ln[5] < 1.05 * size and (
                0.5 * size <= ln[5] < 0.97 * size or ln[0].startswith(_DASHES)) for ln in out[k]) \
                and not out[k][0][0].startswith((CODE_MARK, EQ_MARK, FIGURE_MARK)):
            out[k] = [ln[:5] + (max(ln[5], min(size, body or size)),) + ln[6:] for ln in out[k]]
            epigraph.update(_line_key(pno, ln) for ln in out[k])
            k -= 1
    return out, epigraph


def _mark_drop_chapters(blocks: list[list[tuple]], pno: int, drops: set, epigraph: set) -> list[list[tuple]]:
    """Put a CHAPTER_MARK line before each drop-cap paragraph (and its epigraph) that has no heading of its own
    just above it on the page; a part heading ("BOOK ONE") does not count. A short all-caps line at body size
    alone above it at the top of the page ("INTRODUCTION") is such a heading and is marked as one. Without an
    epigraph ending in an attribution ("—from ...") a drop cap opens a scene, an afterword or a dedication, not
    a chapter: no mark."""
    out = list(blocks)
    d = 0
    while d < len(out):
        blk = out[d]
        if not blk or blk[0][7] or _line_key(pno, blk[0]) not in drops:
            d += 1
            continue
        k = d
        while k > 0 and all(_line_key(pno, ln) in epigraph for ln in out[k - 1]):
            k -= 1
        prev = out[k - 1] if k > 0 else None
        prev_text = " ".join(ln[0] for ln in prev) if prev else ""
        heading = prev is not None and len(prev) == 1 and prev[0][7] \
            and not prev_text.startswith((CODE_MARK, EQ_MARK, FIGURE_MARK))
        if heading and not _PART.match(prev_text):
            d += 1
            continue
        if not heading and prev is not None and len(prev) <= 2 and len(prev_text) <= 60 \
                and prev_text.upper() == prev_text and sum(c.isalpha() for c in prev_text) >= 3 \
                and not prev_text.startswith(_DASHES) \
                and not re.search(r"[.!?,;:]$", prev_text) and min(ln[5] for ln in prev) >= 0.95 * blk[0][5] \
                and k == 1:
            first, last = prev[0], prev[-1]
            out[k - 1] = [(HEADING_MARK + prev_text, first[1], first[2], max(ln[3] for ln in prev), last[4],
                           first[5], first[6], True)]
            d += 1
            continue
        if not any(ln[0].startswith(_DASHES) for b in out[k:d] for ln in b):
            d += 1  # no epigraph with its attribution: a scene break, an afterword or a dedication
            continue
        ln = blk[0]
        out.insert(k, [(CHAPTER_MARK, ln[1], ln[2], ln[3], ln[2], ln[5], False, True)])
        d += 2
    return out


def _number_chapters(paras: list[str]) -> list[str]:
    """CHAPTER_MARK paragraphs as "Chapter N", counted from 1 in the book and again after each part heading or
    introduction (each novel of a collection in one PDF has its own). With fewer than DROP_CHAPTERS of them the
    drop caps are only decoration, and the marks are removed."""
    if sum(p == CHAPTER_MARK for p in paras) < DROP_CHAPTERS:
        return [p for p in paras if p != CHAPTER_MARK]
    out, n = [], 0
    for p in paras:
        if p == CHAPTER_MARK:
            n += 1
            out.append(f"Chapter {n}")
            continue
        if (_PART.match(p) or _OPENING.match(p)) and _is_heading(p):
            n = 0
        out.append(p)
    return out
