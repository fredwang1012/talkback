"""Headings: the heading regex (shared with parsing.parse), headings found by font size, bare chapter numbers
and labels joined to their titles, and the running headers / footers of textbooks."""
import re
import unicodedata
from collections import Counter

from ..parsing import CODE_MARK, EQ_MARK, FIGURE_MARK, HEADING_MARK
from .figures import _in_figure


# A short (< 70 chars) block on its own becomes a section heading: "Chapter 3", "PART II", "CHAPTER ONE", "IV.",
# or "12 The Wedding" (number then capitalised title without sentence punctuation). Books in other languages (#117):
# the same labels in Spanish, French, German, Italian, Portuguese, Dutch, Polish and Russian ("Capítulo 3",
# "Chapitre deux"), and Chinese / Japanese "第三章" / Korean "제3장" with an optional title.
_NUMBER_WORDS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                 r"seventeen|eighteen|nineteen|twenty\S*|thirty\S*|forty\S*|fifty\S*|hundred"
                 r"|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|primero|primera|segundo|segunda|tercero|tercera"
                 r"|un|une|deux|trois|quatre|cinq|sept|huit|neuf|dix|premier|première|deuxième|troisième"
                 r"|eins|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn"
                 r"|primo|prima|secondo|seconda|terzo|terza|quattro|cinque|sei|sette|otto|nove|dieci")
_LABELS = (r"chapter|part|book|section|unit"
           r"|capítulo|capitulo|parte|libro|sección|seccion|unidad|chapitre|partie|livre|tome|kapitel|teil|buch"
           r"|abschnitt|capitolo|sezione|hoofdstuk|deel|boek|rozdział|część|księga|глава|часть|книга")
_CJK_HEADING = r"第[0-9０-９一二三四五六七八九十百千零〇两]+[章回节節部卷篇幕][^。！？]*|제\s*\d+\s*[장부편][^.!?。]*"
HEADING_REGEX = (r"^(?=.{1,69}\Z)(?:(?i:(?:" + _LABELS + r")\s+(?:\d+|[ivxlc]+|" + _NUMBER_WORDS
                 + r")\b[^,]*(?<!-))"
                 r"|[IVXLC]+\.|\d{1,3}\s+[A-Z][^.!?]*|\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+[A-Z][^.!?]*"
                 r"|" + _CJK_HEADING +
                 r"|\x01.+)\Z")  # \x01: a heading found by font size alone (see parsing.HEADING_MARK)
_HEADING = re.compile(HEADING_REGEX)
_BARE_NUMBER = re.compile(r"^\d{1,3}$")  # a chapter number set alone in a huge font above its title
_BARE_LABEL = re.compile(r"^(?i:" + _LABELS + r")\s+(?:\d+|[ivxlc]+|" + _NUMBER_WORDS + r")\s*$", re.I)


HEADING_BIG = 1.15     # a short line this much larger than the body text is a heading
HEADING_BOLD = 1.05    # ... or bold and slightly larger
TOC_RUN = 4            # this many headings in a row is a table of contents, not chapters


def _is_heading(text: str) -> bool:
    return bool(_HEADING.match(text))


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


def _heading_candidate(blk: list[tuple], body: float) -> tuple[str, bool] | None:
    """(text, found by font alone) when the block looks like a heading: the regex, or large / bold short text.

    Copyright, ISBN and publisher lines (front matter) are never headings, however large they are set.
    """
    if len(blk) > 2:
        return None
    text = " ".join(ln[0] for ln in blk)
    if _FRONT_MATTER.search(text) or text.startswith((CODE_MARK, EQ_MARK, FIGURE_MARK)):
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
            out.append([ln[:7] + (ln[0].startswith((CODE_MARK, EQ_MARK, FIGURE_MARK)),) for ln in blk])  # listings, equations, figures stand alone
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
