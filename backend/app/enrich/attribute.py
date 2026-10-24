"""Attribution pass: give untagged dialogue to the right character instead of the narrator.

The segmenter only voices a quote when the paragraph names the speaker ("said Hagrid"). Back-and-forth lines with no tag
stay with the narrator. This pass fixes them in two stages:

1. `attribute_book` (deterministic, free) finds quoted lines the narrator would read and assigns a speaker from
   - a speech tag next to the quote ("said Ron", "Ron muttered"), or a sentence led by a cast name before/after it,
   - paragraph continuity (the same quote resumes after an action beat),
   - alternation in a two-person exchange (an untagged paragraph after A and B have spoken goes to whoever did not speak
     last), vetoed when the line addresses that person by name (vocative).
   A line whose tag names someone outside the cast ("squealed the gnome") is left to the narrator on purpose.
   Lines that stay unsure come back with confidence "ambiguous".
2. `llm_attribute` (optional, paid) asks a model about the ambiguous lines only, with surrounding paragraphs. Answers must
   be a known cast id or they are discarded. It is never run automatically.

Segment texts keep tiling the paragraph verbatim; only speakers change (a narrator segment is split around a quote).
"""
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from ..schema import Book, Segment

VERBS = (r"(?:said|asked|shouted|whispered|snapped|called|muttered|cried|yelled|added|replied|roared|bellowed|hissed|growled|"
         r"sneered|squeaked|gasped|began|answered|says|continued|told|exclaimed|repeated|breathed|sobbed|laughed|interrupted|"
         r"moaned|barked|screamed|stammered|demanded|suggested|whined|sighed|snarled|croaked|spat|panted|groaned|"
         r"bawled|drawled|faltered|insisted|protested|offered|urged|prompted|retorted|squealed|chorused|agreed|"
         r"explained|warned|remarked|observed|announced|declared|admitted|argued|blurted|swore|wondered|mumbled|grunted)\b")
QUOTES = "\"“"
PRONOUN = r"(?:he|she|they|I)"
QUOTE_RE = re.compile(r'"[^"\n]+"|“[^”\n]+”')
MAX_BACK = 6   # how many voiced lines back to look for the other speaker
MAX_GAP = 0    # narration-only paragraphs allowed inside one exchange (alternation needs back-to-back dialogue)
SENT_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Decision:
    para: int
    text: str                 # the quoted line
    speaker: str | None       # None: left to the narrator
    rule: str                 # tag / action / continuity / alternation / uncast / unsure / llm
    confidence: str           # high / medium / ambiguous / uncast
    context: str = ""         # the paragraph (for review and for the LLM pass)


@dataclass
class _Piece:
    kind: str                 # narr / char / cand
    text: str
    speaker: str = "narrator"
    seg: Segment | None = None
    decision: Decision | None = None


class _Names:
    """Alias -> character id; an alias shared by two characters is dropped (never guess between them)."""

    def __init__(self, book: Book):
        owner: dict[str, str | None] = {}
        for c in book.characters:
            for a in {c.name.strip(), *(x.strip() for x in c.aliases)}:
                if len(a) >= 3:
                    owner[a] = c.id if owner.get(a, c.id) == c.id else None
        self.ids = {a: i for a, i in owner.items() if i}
        alt = "|".join(re.escape(a) for a in sorted(self.ids, key=len, reverse=True))
        self.pattern = re.compile(rf"\b(?:{alt})\b") if alt else None

    def find(self, text: str) -> list[tuple[str, int, int]]:
        if not self.pattern:
            return []
        return [(self.ids[m.group(0)], m.start(), m.end()) for m in self.pattern.finditer(text)]


def is_dialogue_quote(q: str) -> bool:
    """A quoted span worth voicing: not a one-word scare quote or title."""
    inner = q[1:-1].strip()
    return len(inner.split()) >= 2 or bool(re.search(r"[.!?,]$", inner))


def _split_narrator(text: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind narr / cand. An odd number of straight quotes (a quote spanning segments) is left alone."""
    if text.count('"') % 2:
        return [("narr", text)]
    out, pos = [], 0
    for m in QUOTE_RE.finditer(text):
        if not is_dialogue_quote(m.group(0)):
            continue
        if text[pos:m.start()].strip():
            out.append(("narr", text[pos:m.start()]))
        out.append(("cand", m.group(0)))
        pos = m.end()
    if text[pos:].strip():
        out.append(("narr", text[pos:]))
    return out or [("narr", text)]


def _pieces(segments: list[Segment]) -> list[_Piece]:
    out: list[_Piece] = []
    for s in segments:
        if s.speaker != "narrator":
            out.append(_Piece("char", s.text.strip(), s.speaker, s))
        else:
            out.extend(_Piece(kind, t.strip(), "narrator", s) for kind, t in _split_narrator(s.text.strip()))
    return out


def addressed(names: _Names, quote: str) -> set[str]:
    """Characters the line speaks TO by name: a leading "Harry, ..." or a closing "..., Harry." (vocative)."""
    inner = quote.strip('"“” ')
    found = set()
    for cid, a, b in names.find(inner):
        before, after = inner[:a].strip(), inner[b:].strip()
        if (a == 0 and after[:1] in {",", "!", "?"}) or (before.endswith(",") and (after[:1] in {",", "!"} or not after.strip(".!?\"” "))):
            found.add(cid)
    return found


ADVERB = r"(?:\w+ly\s+)?"
PERCEPTION = re.compile(r"\b(?:read|wrote|saw|heard|noticed|thought|watched|listened|knew|felt)\b", re.I)


def _after_verb(names: _Names, rest: str) -> tuple[str | None, str]:
    """What follows a speech verb: a cast name (tag), a pronoun/nothing (pronoun), or someone else (foreign)."""
    rest = re.sub(rf"^\s*{ADVERB}", "", rest)
    for cid, a, _ in names.find(rest[:60]):
        if a == 0:
            return cid, "tag"
    if re.match(r"(?:the|a|an|another|some|one|his|her|their|[A-Z][a-z]+)\b", rest) or re.match(r"(?:Professor|Mr|Mrs|Madam)\b", rest):
        return None, "foreign"
    return None, "pronoun"


def _tag_after(names: _Names, text: str) -> tuple[str | None, str]:
    """(speaker, kind) for narration right after a quote. kind: tag ("said Ron", "Ron muttered"), pronoun ("he said"),
    foreign (a tag by someone outside the cast: "squealed the gnome", "said Lockhart") or "" (nothing useful)."""
    head = SENT_END.split(text.strip())[0][:160]
    m = re.match(rf"[,.\s]*{VERBS}\b(.*)", head)
    if m:
        return _after_verb(names, m.group(1))
    m = re.match(r"[,.\s]*[a-z]+(?:ed|ied)\s+(?:[a-z]+ly\s+)?", head)  # a speech verb we do not list: "raged Mr. Weasley"
    if m:
        for cid, a, _ in names.find(head):
            if a == m.end():
                return cid, "tag"
    for cid, a, b in names.find(head):
        if head[:a].strip(" ,") == "":
            if re.match(rf"\s*(?:[\w']+\s+){{0,2}}{VERBS}\b", head[b:]):
                return cid, "tag"
        break
    if re.match(rf"\s*{PRONOUN}\s+(?:\w+\s+)?{VERBS}\b", head, re.I):
        return None, "pronoun"
    if re.match(r"[,.\s]*\w+ed\s+(?:the|a|an|another)\s+\w+", head):  # "mimicked the woman"
        return None, "foreign"
    m = re.match(rf"\s*(?:(?:the|a|an|[A-Z][a-z]+)\s+)(?:[\w']+\s+){{0,3}}{VERBS}\b", head)
    if m:  # "Lockhart said", "a girl with pigtails said": someone outside the cast
        return None, "foreign"
    return None, ""


def _tag_before(names: _Names, text: str) -> tuple[str | None, str]:
    """(speaker, kind) for narration leading into a quote: "Ron said, " (tag) or a short sentence led by a cast name (action)."""
    sents = [s for s in SENT_END.split(text.strip()) if s]
    if not sents:
        return None, ""
    last = sents[-1]
    hits = names.find(last)
    if re.search(rf"{VERBS}\W*$", last) or last.rstrip().endswith((",", ":")):
        for cid, a, b in hits[-1:]:  # the LAST name must sit right at the final speech verb: "Ron said," / "said Ron quietly,"
            if re.fullmatch(rf"\s*(?:[\w']+\s+){{0,2}}{VERBS}\W*", last[b:]) or (
                    re.search(rf"{VERBS}\s+(?:\w+\s+)?$", last[:a]) and re.fullmatch(r"\s*(?:\w+ly)?\W*", last[b:])):
                return cid, "tag"
        if re.match(rf"\W*{PRONOUN}\s+(?:\w+\s+)?{VERBS}", last, re.I):
            return None, "pronoun"
        if re.search(VERBS, last):  # a speech verb whose subject is not in the cast ("the old wizard said in a feeble voice,")
            return None, "foreign"
        return None, ""
    if hits and hits[0][1] == 0 and len(last.split()) <= 10 and not PERCEPTION.search(last) \
            and last.rstrip().endswith((".", "!", "?")):
        return hits[0][0], "action"
    return None, ""


TAG_ONLY = re.compile(rf"^[,.\s]*(?:{PRONOUN}\s+(?:\w+\s+)?|\w+\s+)?{VERBS}\b", re.I)


def infer_genders(book: Book) -> dict[str, str]:
    """Character id -> "he"/"she", from the pronoun of the tag after their already-attributed lines ("..." she said)."""
    count: dict[str, dict[str, int]] = {}
    for p in book.paragraphs:
        for a, b in zip(p.segments, p.segments[1:]):
            if a.speaker != "narrator" and b.speaker == "narrator":
                m = re.match(r"[,.\s]*(he|she)\b", b.text, re.I)
                if m:
                    c = count.setdefault(a.speaker, {"he": 0, "she": 0})
                    c[m.group(1).lower()] += 1
    out = {}
    for cid, c in count.items():
        n = c["he"] + c["she"]
        if n >= 3 and max(c.values()) / n >= 0.85:
            out[cid] = max(c, key=c.get)
    return out


def _pronoun_of(after: str, before: str) -> str | None:
    m = re.match(r"[,.\s]*(he|she)\b", after, re.I) or re.search(r"\b(he|she)\W*$", before, re.I)
    return m.group(1).lower() if m else None


def _decide(names: _Names, cast: set[str], pieces: list[_Piece], i: int,
            history: list[tuple[int, str | None]], starts_exchange: bool, genders: dict[str, str]) -> tuple[str | None, str, str]:
    """(speaker, rule, confidence) for the candidate line pieces[i]."""
    x = pieces[i]
    before = pieces[i - 1].text if i > 0 and pieces[i - 1].kind == "narr" else ""
    after = pieces[i + 1].text if i + 1 < len(pieces) and pieces[i + 1].kind == "narr" else ""
    heard = addressed(names, x.text)
    who_a, kind_a = _tag_after(names, after) if after else (None, "")
    who_b, kind_b = _tag_before(names, before) if before else (None, "")

    speaker, rule, conf = None, "unsure", "ambiguous"
    if "foreign" in (kind_a, kind_b):
        return None, "uncast", "uncast"
    if who_a and kind_a == "tag":
        speaker, rule, conf = who_a, "tag", "high"
    elif who_b and kind_b == "tag":
        speaker, rule, conf = who_b, "tag", "high"
    elif who_b and kind_b == "action" and not any(y.kind in ("cand", "char") for y in pieces[:i]) and len(x.text.split()) >= 3 and x.text.rstrip('"” ').endswith((".", "!", "?", ",")):
        speaker, rule, conf = who_b, "action", "medium"
    else:
        prev = next((j for j in range(i - 1, -1, -1) if pieces[j].kind in ("cand", "char")), None)
        if prev is not None:
            # same paragraph: the quote resumes after "he said, ..." (a tag for the same person), never right after
            # another quote with nothing between (that is an interruption by someone else)
            between = " ".join(y.text for y in pieces[prev + 1:i] if y.kind == "narr")
            if pieces[prev].speaker != "narrator" and between and TAG_ONLY.match(between) and len(between.split()) <= 12                     and not ({cid for cid, _, _ in names.find(between)} - {pieces[prev].speaker}):
                speaker, rule, conf = pieces[prev].speaker, "continuity", "high"
        elif starts_exchange and sum(y.kind in ("cand", "char") for y in pieces) == 1 and history and history[-1][1]:
            a = history[-1][1]
            seq = [s for _, s in history[-3:]]
            if len(seq) >= 2 and seq[-2] and seq[-2] != a and None not in seq:
                b = seq[-2]
                high = (len(seq) >= 3 and seq[-3] == a) or a in heard
                speaker, rule, conf = b, "alternation", "high" if high else "medium"
    pron = _pronoun_of(after, before)
    if speaker and pron and genders.get(speaker, pron) != pron:
        return None, "unsure", "ambiguous"  # "she said" cannot be a character who is always "he"
    if speaker and (speaker in heard or speaker not in cast):
        return None, "unsure", "ambiguous"  # a person does not address themselves by name
    return speaker, rule, conf


def attribute_book(book: Book, apply: bool = True,
                   uncast_speaker: Callable[[str, str, str], str | None] | None = None) -> list[Decision]:
    """Run the heuristics over the whole book; with apply=True the book's segments are updated in place.

    `uncast_speaker(before, quote, after)` (optional) names a stand-in speaker id for a line whose tag points outside
    the cast ("said Mrs. Dursley"); see enrich/minor.py. Such lines get rule "minor" instead of staying "uncast".
    """
    names = _Names(book)
    if not names.pattern:
        return []
    cast = {c.id for c in book.characters}
    genders = infer_genders(book)
    decisions: list[Decision] = []
    history: list[tuple[int, str | None]] = []  # (paragraph idx, speaker) of each voiced line, in order
    last_dialogue = -10
    for p in book.paragraphs:
        pieces = _pieces(p.segments)
        line_idx = [i for i, x in enumerate(pieces) if x.kind in ("cand", "char")]
        if not line_idx:
            continue
        if p.idx - last_dialogue - 1 > MAX_GAP:
            history = []
        # an untagged reply: this paragraph opens with the quote, right after a paragraph that opened with one
        starts_exchange = pieces[0].kind == "cand" and p.idx - 1 == last_dialogue and book.paragraphs[p.idx - 1].text[:1] in QUOTES
        for i, x in enumerate(pieces):
            if x.kind == "char":
                history.append((p.idx, x.speaker))
            elif x.kind == "cand":
                speaker, rule, conf = _decide(names, cast, pieces, i, history[-MAX_BACK:], starts_exchange, genders)
                if rule == "uncast" and uncast_speaker:
                    before = pieces[i - 1].text if i > 0 and pieces[i - 1].kind == "narr" else ""
                    after = pieces[i + 1].text if i + 1 < len(pieces) and pieces[i + 1].kind == "narr" else ""
                    speaker = uncast_speaker(before, x.text, after)
                    if speaker:
                        rule, conf = "minor", "high"
                x.decision = Decision(p.idx, x.text, speaker, rule, conf, p.text)
                decisions.append(x.decision)
                if speaker:
                    x.speaker = speaker
                history.append((p.idx, speaker))
        last_dialogue = p.idx
        if apply and any(x.decision and x.decision.speaker for x in pieces):
            p.segments = _rebuild(pieces, p.text) or p.segments
    return decisions


def _rebuild(pieces: list[_Piece], text: str) -> list[Segment] | None:
    """Segments from pieces: character lines keep their segment, newly attributed quotes become segments, narration
    merges. Merged narration is cut from the paragraph `text` itself (what separates two pieces is not always one
    space: 'shrugged—"nothing', 'dark," he'), so the result tiles it verbatim. None if a piece is not found in order."""
    out: list[Segment] = []
    starts: list[int] = []   # paragraph offset where each out segment starts
    cursor = 0
    for x in pieces:
        if not x.text:
            continue
        at = text.find(x.text, cursor)
        if at < 0:
            return None
        cursor = at + len(x.text)
        if x.kind == "char":
            out.append(x.seg)  # type: ignore[arg-type]
        elif x.kind == "cand" and x.speaker != "narrator":
            out.append(Segment(speaker=x.speaker, text=x.text))
        elif out and out[-1].speaker == "narrator":
            out[-1] = Segment(speaker="narrator", text=text[starts[-1]:cursor])
            continue
        else:
            out.append(Segment(speaker="narrator", text=x.text))
        starts.append(at)
    return out


def summarize(decisions: list[Decision]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in decisions:
        key = d.confidence if d.speaker is None else f"{d.confidence}/{d.rule}"
        out[key] = out.get(key, 0) + 1
    out["lines"] = len(decisions)
    out["reassigned"] = sum(1 for d in decisions if d.speaker)
    return out


# ---------- optional LLM pass for what the heuristics could not settle (never run automatically) ----------

class LineAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int
    speaker: str   # a cast id, or "narrator" when the line is spoken by someone outside the cast


class LineAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[LineAnswer]


LLM_SYSTEM = """You attribute lines of dialogue in a novel to the character who speaks them.
For each numbered line you get the paragraph it is in and the paragraphs before it.
Answer with the speaker's id from the cast list, or "narrator" if the speaker is not in the cast or you cannot tell.
Use only the text given. Return one entry per line number."""

LLM_BATCH = 12
CONTEXT_BEFORE = 3


def llm_user(book: Book, batch: list[Decision]) -> str:
    cast = "\n".join(f'- id "{c.id}": {c.name} (also called: {", ".join(c.aliases)})' for c in book.characters)
    blocks = []
    for n, d in enumerate(batch):
        before = "\n".join(q.text for q in book.paragraphs[max(0, d.para - CONTEXT_BEFORE):d.para])
        blocks.append(f"### line {n}\nPrevious paragraphs:\n{before}\n\nParagraph:\n{d.context}\n\nLine to attribute: {d.text}")
    return f"Cast:\n{cast}\n\n" + "\n\n".join(blocks)


def llm_estimate(items: list[tuple[Book, list[Decision]]], in_price: float = 0.30, out_price: float = 2.50) -> dict:
    """Rough Gemini Flash cost (USD per 1M tokens in/out) of attributing the given ambiguous lines (~4 chars/token)."""
    chars = out_tokens = calls = 0
    for book, ds in items:
        for i in range(0, len(ds), LLM_BATCH):
            b = ds[i:i + LLM_BATCH]
            chars += len(LLM_SYSTEM) + len(llm_user(book, b))
            out_tokens += 12 * len(b) + 20
            calls += 1
    in_tokens = chars // 4
    return {"calls": calls, "in_tokens": in_tokens, "out_tokens": out_tokens,
            "usd": (in_tokens * in_price + out_tokens * out_price) / 1e6}


async def llm_attribute(book: Book, ambiguous: list[Decision], call: Callable[..., Awaitable[LineAnswers]],
                        model: str = "fast") -> int:
    """Ask the model about ambiguous lines and apply valid answers to the book. Returns how many lines changed."""
    cast = {c.id for c in book.characters}
    changed = 0
    for i in range(0, len(ambiguous), LLM_BATCH):
        batch = ambiguous[i:i + LLM_BATCH]
        res = await call(model, LLM_SYSTEM, llm_user(book, batch), LineAnswers, max_tokens=800 + 40 * len(batch), effort="low")
        for a in res.lines:
            if 0 <= a.n < len(batch) and a.speaker in cast:
                d = batch[a.n]
                d.speaker, d.rule, d.confidence = a.speaker, "llm", "medium"
                changed += 1
    by_para: dict[int, dict[str, str]] = {}
    for d in ambiguous:
        if d.rule == "llm" and d.speaker:
            by_para.setdefault(d.para, {})[d.text] = d.speaker
    for idx, want in by_para.items():
        p = book.paragraphs[idx]
        pieces = _pieces(p.segments)
        for x in pieces:
            if x.kind == "cand" and x.text in want:
                x.speaker = want[x.text]
        p.segments = _rebuild(pieces, p.text) or p.segments
    return changed
