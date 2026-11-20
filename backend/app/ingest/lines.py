"""A page's text lines and how consecutive lines merge into paragraphs (see pdf.pdf_to_text).

Each PDF line reaches merge_lines as a Line; the page pipeline before that works on bare tuples
(text, x0, y0, x1, y1, size, bold, flag), where `flag` is "monospace" until headings._mark_headings turns it into
"stands alone" (a heading, listing, equation or figure).
"""
import re
from typing import NamedTuple

from ..parsing import FIGURE_MARK


_PAGE_NUMBER = re.compile(r"^\W*(?:page\s+)?(?:\d{1,5}|[ivxlc]{1,7})\W*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"""[.!?。！？]["'”’)\]»」』）]*$""")
# Chinese / Japanese characters and full-width punctuation: lines of such text join without a space (#117)
_CJK_CHAR = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
INDENT_PT = 6.0       # a line starting this far right of the page's left margin is indented
GAP_FACTOR = 0.5      # a vertical gap above this fraction of the line height is a paragraph gap
SHORT_FRACTION = 0.75  # a sentence-ending line shorter than this fraction of the text width ends a paragraph


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
    drop: bool = False         # this line opens with a drop cap (see _merge_drop_caps): it starts a paragraph


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")  # math fonts (CMEX10 brackets) extract as control codes such as \x01


def _clean_line_text(text: str) -> str:
    """A line's text without ligatures or control codes; "\x01" is the parser's heading mark and must never leak in."""
    return re.sub(r"\s{2,}", " ", _CONTROL.sub(" ", text.translate(_LIGATURES))).strip()


def _norm_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.lower())).strip()


def _starts_paragraph(prev: Line, cur: Line) -> bool:
    if prev.alone or cur.alone or cur.drop:
        return True
    if not _SENTENCE_END.search(prev.text) or cur.text[:1].islower():
        return False  # mid-sentence, or a lowercase continuation such as '" said Ron.'
    if cur.x0 > cur.left + INDENT_PT:
        return True
    if cur.page == prev.page and cur.y0 - prev.y1 > GAP_FACTOR * max(prev.y1 - prev.y0, 1.0):
        return True
    return prev.x1 < prev.left + SHORT_FRACTION * (prev.right - prev.left)


def _join_lines(parts: list[str]) -> str:
    """The lines of one paragraph joined by spaces, except between two Chinese / Japanese characters (a line break
    inside CJK text is no word break)."""
    out = [parts[0]]
    for a, b in zip(parts, parts[1:]):
        if not (_CJK_CHAR.match(a[-1:]) and _CJK_CHAR.match(b[:1])):
            out.append(" ")
        out.append(b)
    return "".join(out)


def merge_lines(lines: list[Line]) -> list[str]:
    """Group consecutive lines into paragraphs (linear: each paragraph's parts are joined once).

    A figure (FIGURE_MARK line) is a paragraph of its own; one met mid-sentence (it sits at the top of the page a
    sentence runs on to) is placed after the paragraph it interrupts, so the sentence stays whole."""
    paras: list[str] = []
    parts: list[str] = []
    held: list[str] = []
    prev: Line | None = None

    def flush() -> None:
        if parts:
            paras.append(_join_lines(parts))
            parts.clear()
        paras.extend(held)
        held.clear()

    for ln in lines:
        if ln.text.startswith(FIGURE_MARK):
            if parts and prev is not None and not _SENTENCE_END.search(prev.text):
                held.append(ln.text)
            else:
                flush()
                paras.append(ln.text)
            continue
        if prev is not None and _starts_paragraph(prev, ln):
            flush()
        if parts and parts[-1].endswith("-") and len(parts[-1]) > 1 and parts[-1][-2].isalpha() \
                and ln.text[:1].islower():
            parts[-1] = parts[-1][:-1] + (ln.marked or ln.text)  # de-hyphenate "exam-" + "ple"
        else:
            parts.append(ln.marked or ln.text)
        prev = ln
    flush()
    return paras
