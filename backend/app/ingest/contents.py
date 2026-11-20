"""Tables of contents: pages of entries (with or without dot leaders) are dropped, never read as chapters."""
import re

from ..parsing import EQ_MARK


_CONTENTS = re.compile(r"^(?:table of\s+)?contents$|^(?:índice|indice|contenido|table des matières|sommaire|"
                       r"inhalt|inhaltsverzeichnis|sommario|inhoud|spis treści|содержание|目录|目錄|目次|목차)$", re.I)


def _toc_style(blocks: list[list[tuple]]) -> tuple[set, list[float]]:
    """(font sizes, left edges) of the lines of a contents page."""
    lines = [ln for b in blocks for ln in b]
    return {ln[5] for ln in lines}, sorted({round(ln[1], 1) for ln in lines})


def _is_toc_tail(blocks: list[list[tuple]], style: tuple[set, list[float]]) -> bool:
    """A page holding only the last entries of the contents page before it ("Epilogue"): every line short, not a
    sentence, and set in the font size and at a left edge of that page's entries."""
    sizes, lefts = style
    lines = [ln for b in blocks for ln in b]
    return bool(lines) and len(lines) <= 40 and all(
        len(ln[0]) <= 60 and not re.search(r"[.!?]$", ln[0]) and ln[5] in sizes
        and any(abs(ln[1] - x) <= 3 for x in lefts) for ln in lines)


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
        # a line led by a dash is an epigraph's attribution ("—ARCHIVES #X99 . . . .. 041"), never an entry
        blk = [ln for ln in blk if ln[0].startswith((EQ_MARK, "—", "―")) or not _has_dot_leader(ln[0])]
        if blk:
            out.append(blk)
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
