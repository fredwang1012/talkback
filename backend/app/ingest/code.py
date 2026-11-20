"""Program and pseudocode listings: a run of code-like blocks becomes one CODE_MARK line (shown, not read)."""
import re

from ..parsing import CODE_MARK


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
