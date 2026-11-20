"""Figures: labelled drawing / image regions (their text is never a heading) and captioned figures cropped to a PNG
for the reader (#40)."""
import bisect
import re
from pathlib import Path

from ..config import settings
from ..parsing import EQ_MARK, FIGURE_MARK
from ..pdfdoc import open_pdf
from .layout import COLUMN_GAP, _LEADING_SYMBOLS


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


def _page_figures(page, blocks: list[list[tuple]], rects: list[tuple]) -> list[tuple]:
    """Labelled figure regions of a page (a pdfdoc.Page): its images (`rects`) plus its clustered vector drawings,
    within bounds (past a bound, or with a broken content stream, there is no figure detection: the text is still
    read)."""
    if not blocks or len(rects) > MAX_FIGURE_RECTS:
        return []
    clusters = page.drawing_clusters(MAX_CONTENT_BYTES, MAX_DRAWINGS)
    if clusters is None:
        return []
    return _figure_regions(rects + clusters, [ln for blk in blocks for ln in blk], page.area)


def _in_figure(blk: list[tuple], figures: list[tuple]) -> bool:
    return any(_overlaps(f, ln[1:5]) for f in figures for ln in blk)


# ---- figures shown in the reader (#40) ----
# A figure is found from its caption: "Figure 2.10 ..." set small or bold, and everything between it and the nearest
# prose above it (else below it) is the figure. That finds figures built from text, rules and tiny images (the IR
# textbook's matrices and posting lists have no vector drawings at all) as well as embedded images and diagrams.
_FIGURE_CAPTION = re.compile(r"^(?:figure|fig\.)\s+\d+(?:[.-]\d+)*\b", re.I)
_FIGURE_LABEL = re.compile(r"^((?:figure|fig\.)\s+\d+(?:[.-]\d+)*)\.?\s*[:|–—]?\s*", re.I)
PROSE_WIDTH = 0.6        # a body line at least this share of the column wide, starting at its left edge (or
PROSE_WORDS = 5          # indented) and reaching its right edge, with at least this many words, is prose
PROSE_INDENT = 20.0      # points: a paragraph indent (and the shift between odd and even pages)
PROSE_EDGE = 8.0         # points short of the right edge that still count as reaching it
COLUMN_PROSE = 5         # a page with this many wordy body lines in each half is set in two columns
FIGURE_PAD = 4.0         # points around a figure's content in the crop (box borders, braces)
FIGURE_MIN_HEIGHT = 12.0  # points: a smaller region is no figure (a caption with nothing next to it)
FIGURE_DPI = 150
FIGURE_MAX_PX = 2000     # longest side of a figure image; a huge page is rendered at a lower resolution
MAX_FIGURES = 500        # per book: past this, captions are dropped as before


def figures_dir(book_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "figures"


def _captions(blocks: list[list[tuple]], body: float) -> list[tuple[int, int, int]]:
    """(block, first line, end line) of each figure caption: a line starting "Figure N" (after any marker such as ◮)
    set smaller than the body or bold, plus the small lines close below it that continue it."""
    out = []
    for bi, blk in enumerate(blocks):
        i = 0
        while i < len(blk):
            ln = blk[i]
            if (ln[5] <= body * 0.95 or ln[6]) and _FIGURE_CAPTION.match(_LEADING_SYMBOLS.sub("", ln[0])):
                j = i + 1
                while j < len(blk) and blk[j][5] <= body * 0.95 and blk[j][2] - blk[j - 1][4] < 1.5 * (blk[j][4] - blk[j][2]):
                    j += 1
                out.append((bi, i, j))
                i = j
            else:
                i += 1
    return out


def _caption_text(lines: list[tuple]) -> str:
    """'◮Figure 1.3 The two parts ...' -> 'Figure 1.3: The two parts ...' (read aloud as written)."""
    text = re.sub(r"\s+", " ", _LEADING_SYMBOLS.sub("", " ".join(ln[0] for ln in lines))).strip()
    return _FIGURE_LABEL.sub(lambda m: m.group(1) + ": ", text, count=1).rstrip(": ")


def _text_column(pages: list[list[list[tuple]]], body: float) -> tuple[float, float] | None:
    """(left, right) edge of the body text column over the whole document: a page that is mostly figure says
    nothing about it, so it is never measured per page."""
    lines = [ln for blocks in pages for blk in blocks for ln in blk if body and ln[5] >= body * 0.95
             and not ln[0].startswith(EQ_MARK) and len(ln[0].split()) >= PROSE_WORDS]  # an equation line is no prose
    if len(lines) < 3:
        return None
    return sorted(ln[1] for ln in lines)[len(lines) // 5], sorted(ln[3] for ln in lines)[len(lines) * 9 // 10]


def _two_column_prose(blocks: list[list[tuple]], body: float, width: float) -> bool:
    """Prose on both sides of the page middle (a figure of many small cells is not two columns)."""
    mid = width / 2
    wordy = [ln for blk in blocks for ln in blk
             if ln[5] >= body * 0.95 and not ln[0].startswith(EQ_MARK) and len(ln[0].split()) >= PROSE_WORDS]
    return sum(ln[3] < mid + COLUMN_GAP / 2 for ln in wordy) >= COLUMN_PROSE \
        and sum(ln[1] > mid - COLUMN_GAP / 2 for ln in wordy) >= COLUMN_PROSE


def _take_figures(blocks: list[list[tuple]], rects: list[tuple], page_box: tuple, body: float,
                  column: tuple[float, float] | None, render) -> list[list[tuple]]:
    """Replace each captioned figure of a page (its caption and the text, rules and images making it up) with one
    FIGURE_MARK line where the figure starts. `render(rect)` saves the crop and returns its file name, or None: the
    figure's text is dropped either way (it is never body text), only a rendered figure gets a paragraph.
    Two-column pages are left alone (their column is not the document's): captions are dropped as before."""
    if not blocks or not body or column is None or _two_column_prose(blocks, body, page_box[2] - page_box[0]):
        return blocks
    caps = _captions(blocks, body)
    if not caps:
        return blocks
    left, right = column
    width = max(right - left, 1.0)
    page_area = (page_box[2] - page_box[0]) * (page_box[3] - page_box[1])

    def prose(ln) -> bool:
        return ln[5] >= body * 0.95 and not ln[0].startswith(EQ_MARK) and ln[1] <= left + PROSE_INDENT and ln[3] >= right - PROSE_EDGE \
            and ln[3] - ln[1] >= PROSE_WIDTH * width and len(ln[0].split()) >= PROSE_WORDS

    cap_lines = {(bi, li) for bi, i, j in caps for li in range(i, j)}
    # a block with a prose line is a paragraph: its short last line ("observation and experiment.") is prose too
    prose_blocks = {bi for bi, blk in enumerate(blocks) if any(prose(ln) for li, ln in enumerate(blk) if (bi, li) not in cap_lines)}
    taken: set[tuple[int, int]] = set()
    marks: dict[int, list[list[tuple]]] = {}
    every = [(bi, li, ln) for bi, blk in enumerate(blocks) for li, ln in enumerate(blk)]
    for bi, i, j in sorted(caps, key=lambda c: blocks[c[0]][c[1]][2]):
        cap = blocks[bi][i:j]
        cy0, cy1 = min(ln[2] for ln in cap), max(ln[4] for ln in cap)
        free = [(b, l, ln) for b, l, ln in every if (b, l) not in taken and (b, l) not in cap_lines]
        content: list[tuple[int, int, tuple]] = []
        parts: list[tuple] = []  # rules, boxes and pictures in the figure's band
        for side in ("above", "below"):
            above = side == "above"
            near = sorted(((b, l, ln) for b, l, ln in free if (ln[4] <= cy0 + 1 if above else ln[2] >= cy1 - 1)),
                          key=lambda t: -t[2][4] if above else t[2][2])
            edge = page_box[1] if above else page_box[3]  # the prose the walk stops at, else the page edge
            for b, l, ln in near:
                # below a caption only labels, cells or a picture can be its figure: any sentence is body text
                if b in prose_blocks or prose(ln) or (not above and ln[5] >= body * 0.95 and len(ln[0].split()) >= PROSE_WORDS):
                    edge = ln[4] if above else ln[2]
                    break
                content.append((b, l, ln))
            band0, band1 = (edge - 1, cy0 + 1) if above else (cy1 - 1, edge + 1)
            parts = [r for r in rects if r[1] >= band0 and r[3] <= band1
                     and (r[2] - r[0]) * (r[3] - r[1]) <= FIGURE_MAX_AREA * page_area]
            picture = any(r[2] - r[0] >= THIN and r[3] - r[1] >= THIN for r in parts)
            if content or picture:
                break
            parts = []
        if not content and not parts:
            continue  # a caption with nothing next to it: _clean_layout drops it as before
        boxes = [ln[1:5] for _, _, ln in content] + [tuple(r) for r in parts]
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        box = (max(page_box[0], x0 - FIGURE_PAD), max(page_box[1], y0 - FIGURE_PAD),
               min(page_box[2], x1 + FIGURE_PAD), min(page_box[3], y1 + FIGURE_PAD))
        if box[3] - box[1] < FIGURE_MIN_HEIGHT or box[2] <= box[0]:
            continue
        taken.update((b, l) for b, l, _ in content)
        taken.update((bi, li) for li in range(i, j))
        name = render(box)
        if name:
            first = min(b for b, _, _ in content + [(bi, i, None)])
            marks.setdefault(first, []).append(
                [(FIGURE_MARK + name + "\n" + _caption_text(cap), box[0], box[1], box[2], box[3], body, False, False)])
    out: list[list[tuple]] = []
    for bi, blk in enumerate(blocks):
        out.extend(marks.get(bi, []))
        rest = [ln for li, ln in enumerate(blk) if (bi, li) not in taken]
        if rest:
            out.append(rest)
    return out


class _FigureRenderer:
    """Crops figure regions out of the PDF into PNGs named fig-1.png, fig-2.png, ... (at most MAX_FIGURES)."""

    def __init__(self, data: bytes, out_dir: Path | None):
        self.out_dir, self.count, self.doc = out_dir, 0, None
        if out_dir is not None:
            self.doc = open_pdf(data)

    def page(self, pno: int):
        return lambda box: self.render(pno, box)

    def render(self, pno: int, box: tuple) -> str | None:
        if self.doc is None or self.count >= MAX_FIGURES:
            return None
        w, h = box[2] - box[0], box[3] - box[1]
        zoom = min(FIGURE_DPI / 72, FIGURE_MAX_PX / max(w, h, 1.0))
        try:
            raster = self.doc.page(pno).render(box, zoom=zoom)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            name = f"fig-{self.count + 1}.png"
            raster.save(self.out_dir / name)
        except Exception:  # an unrenderable region: the figure is skipped, the text is still read
            return None
        self.count += 1
        return name

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()
