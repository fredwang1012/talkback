"""Display equations (#41) and inline formulas: math-font spans, the visual rows pymupdf splits around tall glyphs,
and the EQ_MARK / inline-marker lines the page pipeline keeps in place of them."""
import re
import unicodedata

from ..parsing import EQ_MARK, EQ_SEP, inline_marker, strip_inline
from .code import _code_score
from .layout import _MONO_FONT
from .lines import _CONTROL, _LIGATURES, _clean_line_text


# ---- display equations (#41) ----
# TeX math italic / symbols / big operators (CMMI, CMSY, CMEX and their AMS / Latin Modern / tx / px kin), OpenType
# math fonts (Cambria Math, STIX, XITS...) and the classic Symbol / MT Extra fonts of word processors.
_MATH_FONT = re.compile(
    r"^(?:[A-Z]{6}\+)?(?:CM(?:MI|SY|EX|BSY|MIB|BMI)\d|MSAM|MSBM|EU(?:FM|FB|SM|SB|EX)|RSFS|STIX|XITS|Asana|Euclid|"
    r"MathematicalPi|Symbol|MT-?Extra|(?:rtx|ntx|tx|px|zx)(?:mi|sy|ex|ia)|esint|stmary|wasy|.*math)", re.I)
_BIG_OP_FONT = re.compile(r"^(?:[A-Z]{6}\+)?(?:CMEX|EUEX|(?:rtx|ntx|tx|px)ex)", re.I)
_PROSE_WORD = re.compile(r"[a-z]{2,}")   # "we", "known": a word, not a math italic y or an index i
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
    ops = [s for s in real if _BIG_OP_FONT.search(s["font"])]
    if ops:
        # the raw box, not a band around the reported baseline: that baseline sits at the top of a tall
        # delimiter, and a band around it lands on the sentence above. An exponent drawn beside the
        # delimiter must not pull the anchor back up to that baseline.
        return (min(s["bbox"][1] for s in ops) + max(s["bbox"][3] for s in ops)) / 2
    return base


def _attach_deco_rows(groups: list[list[list]]) -> None:
    """Move a math-only row onto the prose row it belongs to.

    pymupdf puts a root or a hat in its own block, so the row assembly inside one block never sees the words beside
    it. `groups` is per block a list of [spans, baseline], edited in place.
    """
    prose = [(bi, li) for bi, blk in enumerate(groups) for li, (spans, _b) in enumerate(blk)
             if _math_class(spans) == NOT_MATH]
    prose_set = set(prose)
    for bi, blk in enumerate(groups):
        for li, item in enumerate(blk):
            # a prose row stays prose even after a glyph is appended: reclassifying it would delete the sentence
            if item is None or (bi, li) in prose_set:
                continue
            spans, base = item
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
