"""Page layout cleanup: fonts, captions, footnotes, runs of table cells and two-column reading order."""
import re


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


def _column_blocks(blocks: list[list[tuple]], width: float) -> tuple[list, list] | None:
    """(left column blocks, right column blocks) of a clearly two-column page, else None."""
    mid = width / 2
    left = [b for b in blocks if max(ln[3] for ln in b) < mid + COLUMN_GAP / 2]
    right = [b for b in blocks if min(ln[1] for ln in b) > mid - COLUMN_GAP / 2]
    if len(left) < 2 or len(right) < 2 or len(left) + len(right) < len(blocks) * 0.8:
        return None
    return left, right


def _reading_order(blocks: list[list[tuple]], width: float) -> list[list[tuple]]:
    """Reorder blocks of a clearly two-column page: left column, then right, between full-width blocks."""
    cols = _column_blocks(blocks, width)
    if cols is None:
        return blocks
    left, right = cols
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
