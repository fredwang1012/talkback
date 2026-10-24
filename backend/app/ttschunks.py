"""Split paragraphs into Boson TTS chunks (<= 280 chars, sentence-aligned). Shared by preprocess/chunk_tts.py and uploads."""
import re

from .delivery import SONG_MAX_PARAS, apply_styles, detect_expressive, is_scene_break, speech_tag, sung_from, with_sfx
from .schema import Book, Paragraph, Segment, TtsChunk
from .textnorm import normalize_for_tts

MAX_CHARS = 280
# Bump whenever chunk_paragraph's output can change (tags, pauses, sfx, splitting, normalisation): every book built
# with a lower Book.chunk_rules is re-chunked once by the server (books.load_book). History:
#   1: expressive delivery (#73: lead pauses, sfx, expressive_*) and singing (#74). Books without the field are 0.
#   2: sneeze and humming sound effects (#135).
CHUNK_RULES_VERSION = 2
DEFAULT_VOICE = "default"
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "messrs", "capt", "col", "prof", "no", "vs", "etc", "sr", "jr", "gen", "lt"}

_SENT_END = re.compile(r"""(?<![.!?])[.!?]++["')\]]*+(?=\s)""")  # lookbehind + possessive: linear on punctuation runs


_WS = re.compile(r"\s*")
_NONWS = re.compile(r"\S")
_LAST_WORD = re.compile(r"[A-Za-z]+\.?$")
_INITIAL = re.compile(r"(?:^|\s)[A-Z]\.$")
_TAIL = 40  # abbreviation / initial checks only need the last few characters before the period


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) offsets of sentences; spans cover all non-whitespace text. Linear in len(text)."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END.finditer(text):
        if m.start() < start:
            continue
        end = m.end()
        before = text[max(start, m.start() + 1 - _TAIL):m.start() + 1]
        last_word = _LAST_WORD.search(before)
        word = last_word.group().rstrip(".").lower() if last_word else ""
        nxt = _WS.match(text, end).end()
        if m.group().rstrip("\"')]") == ".":  # a lone period, not "?!" or an ellipsis
            if word in ABBREVIATIONS or _INITIAL.search(before):
                continue  # "Mr.", initials such as "J. Smith"
        if text[nxt:nxt + 1].islower():
            continue  # ellipsis, dialogue tag ('"Well?" said he.'), or other lowercase continuation
        spans.append((start, end))
        start = nxt
    first = _NONWS.search(text, start)
    if first:
        spans.append((start, len(text.rstrip())))
    return spans


def split_long(text: str, base: int, limit: int = MAX_CHARS) -> list[tuple[int, int]]:
    """Split an over-long sentence at the last comma/semicolon/colon/dash (else space) before `limit`."""
    out: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while n - pos > limit:
        window = text[pos:pos + limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "), window.rfind("—"))
        cut = cut + 1 if cut > limit // 3 else window.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append((base + pos, base + pos + cut))
        pos += cut
        while pos < n and text[pos] == " ":
            pos += 1
    if pos < n:
        out.append((base + pos, base + n))
    return out


def build_tts_text(spoken: str, voice_tags: str, emotion: str | None, style: str | None = None,
                   sfx: bool = False, *, tag: str = "", expressive: str | None = None, pause: str | None = None) -> str:
    """[voice prosody tags][style][emotion][expressiveness][pause][spoken text, with an sfx before onomatopoeia if sfx].

    Delivery tags (style, emotion, expressive_*) go at the start; `pause` ('pause' / 'long_pause') is positional and sits
    right before the text. `tag` is the adjacent speech tag, which some sound effects need (see delivery.sfx_at).
    An expressive_* already in the voice tags is not repeated."""
    if expressive and "expressive_" in voice_tags:
        expressive = None
    tags = (voice_tags + (f"<|style:{style}|>" if style else "") + (f"<|emotion:{emotion}|>" if emotion else "")
            + (f"<|prosody:{expressive}|>" if expressive else "") + (f"<|prosody:{pause}|>" if pause else ""))
    text = normalize_for_tts(spoken)
    return tags + (with_sfx(text, tag) if sfx else text)


class BookIndex:
    """What lead_pause / sing_offset look up for every paragraph, built once per book: the section start paragraphs
    and paragraph idx -> paragraph (rechunk_book on 30k paragraphs x 3k sections went from ~10 s to linear)."""

    def __init__(self, book: Book):
        self.book, self.paragraphs, self.sections = book, book.paragraphs, book.sections
        self.n_paragraphs, self.n_sections = len(book.paragraphs), len(book.sections)
        self.starts = frozenset(s.start_para for s in book.sections)
        self.by_idx: dict[int, Paragraph] = {}
        for q in book.paragraphs:
            self.by_idx.setdefault(q.idx, q)   # the first paragraph with an idx, as the old linear scan found

    def fits(self, book: Book) -> bool:
        return (self.book is book and self.paragraphs is book.paragraphs and self.sections is book.sections
                and self.n_paragraphs == len(book.paragraphs) and self.n_sections == len(book.sections))


_last_index: BookIndex | None = None   # single-paragraph calls (no index passed) on the same book reuse one index


def book_index(book: Book) -> BookIndex:
    global _last_index
    idx = _last_index
    if idx is None or not idx.fits(book):
        idx = _last_index = BookIndex(book)
    return idx


def _prev_paragraph(p: Paragraph, book: Book, index: BookIndex | None = None) -> Paragraph | None:
    ps = book.paragraphs
    if 0 < p.idx <= len(ps) and ps[p.idx - 1].idx == p.idx - 1:
        return ps[p.idx - 1]
    return (index or book_index(book)).by_idx.get(p.idx - 1)


def lead_pause(p: Paragraph, book: Book, index: BookIndex | None = None) -> str | None:
    """Positional pause before a paragraph's first chunk (#73), or None.

    long_pause: first paragraph of a section (after the chapter heading) or right after a scene break ('* * *').
    pause: a dialogue paragraph right after a paragraph of pure narration that ended a sentence."""
    if is_scene_break(p.text):
        return None
    index = index or book_index(book)
    if p.idx in index.starts:
        return "long_pause"
    prev = _prev_paragraph(p, book, index)
    if prev is None or prev.kind == "code":
        return None
    if is_scene_break(prev.text):
        return "long_pause"
    chars = {c.id for c in book.characters}
    first = next((s for s in p.segments if s.text.strip()), None)
    if first is None or first.speaker not in chars:
        return None
    prev_segs = [s for s in prev.segments if s.text.strip()]
    if prev_segs and all(s.speaker not in chars for s in prev_segs) and prev.text.rstrip().rstrip("\"'”’)").endswith((".", "!", "?")):
        return "pause"
    return None


def _groups(p: Paragraph, text: str, base: int) -> list[tuple[int, int]]:
    """Whole sentences packed greedily up to MAX_CHARS (over-long sentences split first), as paragraph offsets.
    Inline math (#41) is measured by its spoken form."""
    pieces: list[tuple[int, int]] = []
    for s, e in sentence_spans(text):
        if e - s > MAX_CHARS:
            pieces += split_long(text[s:e], base + s)
        else:
            pieces.append((base + s, base + e))
    if p.inline_math:
        pieces = [q for s, e in pieces for q in _fit_pieces(p, s, e)]
    groups: list[tuple[int, int]] = []
    for s, e in pieces:
        if groups and len(spoken_text(p, groups[-1][0], e)) <= MAX_CHARS:
            groups[-1] = (groups[-1][0], e)
        else:
            groups.append((s, e))
    return groups


def sing_offset(p: Paragraph, book: Book, index: BookIndex | None = None) -> int | None:
    """Offset in p.text from which the paragraph is sung (#74), or None. Looks back over the same chapter only."""
    if p.kind == "code" or len(p.text) > 4000:
        return None
    index = index or book_index(book)
    prev: list[str] = []
    cur = p
    while len(prev) <= SONG_MAX_PARAS and cur.idx not in index.starts:
        cur = _prev_paragraph(cur, book, index)
        if cur is None:
            break
        prev.insert(0, cur.text)
    return sung_from(prev, p.text)


CODE_SPOKEN = "Code listing skipped."
EQUATION_SPOKEN = "Equation."


def _fit(text: str, limit: int = MAX_CHARS) -> str:
    """`text` cut at the last clause / word boundary so that it fits in one TTS chunk."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    cut = max(head.rfind(", "), head.rfind("; "))
    cut = cut if cut > limit // 2 else head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip(" ,;") + "."


def equation_tts_text(p: Paragraph) -> str:
    """What is said for a display equation: the model's reading, else the extracted text when it reads faithfully,
    else just "Equation 3.4." One chunk (<= MAX_CHARS after normalization)."""
    from .parsing import split_equation_number
    m = p.math
    number = m.number if m else None
    head, *rest = number.split(".") if number else [""]
    label = " point ".join(["Equation " + head] + [" ".join(r) for r in rest]) if number else "Equation"
    if m is not None and m.spoken:
        spoken = f"{label}: {m.spoken.strip().rstrip('.')}."
    elif m is not None and not m.garbled:
        spoken = split_equation_number(p.text)[0] or EQUATION_SPOKEN
    else:
        spoken = f"{label}."
    return _fit(build_tts_text(spoken, "", None))


INLINE_SPOKEN_MAX = 120  # a longer reading of an inline formula is not used (the shown text is read instead)


def inline_spans(p: Paragraph, s: int, e: int) -> list[tuple[int, int, str]]:
    """(start, end, spoken) relative to `s` of the inline formulas inside p.text[s:e] that have a usable reading."""
    return [(m.start - s, m.end - s, m.spoken.strip()) for m in p.inline_math
            if s <= m.start and m.end <= e and m.spoken and m.spoken.strip()
            and len(m.spoken.strip()) <= INLINE_SPOKEN_MAX]


def spoken_text(p: Paragraph, s: int, e: int) -> str:
    """p.text[s:e] with each inline formula replaced by its reading."""
    text = p.text[s:e]
    for a, b, spoken in reversed(inline_spans(p, s, e)):
        text = text[:a] + spoken + text[b:]
    return text


def _fit_pieces(p: Paragraph, s: int, e: int) -> list[tuple[int, int]]:
    """Split [s, e) until each piece's spoken text fits in MAX_CHARS."""
    limit = e - s
    while len(spoken_text(p, s, e)) > MAX_CHARS and limit > MAX_CHARS // 4:
        limit -= MAX_CHARS // 8
        pieces = split_long(p.text[s:e], s, limit)
        if all(len(spoken_text(p, a, b)) <= MAX_CHARS for a, b in pieces):
            return pieces
    return [(s, e)]


def chunk_paragraph(p: Paragraph, book: Book, *, expressive: bool = True, index: BookIndex | None = None) -> list[TtsChunk]:
    """TTS chunks for one paragraph. expressive=False leaves out the #73 additions (pauses, expressive_*, sfx other
    than laughter context), which scripts/estimate_expressive.py uses to measure what they change. `index` (from
    book_index) saves the per-book lookups when chunking many paragraphs."""
    narrator_voice = book.narrator.voice if book.narrator else DEFAULT_VOICE
    if p.kind == "code":  # a listing is displayed whole but spoken as one short note
        return [TtsChunk(id=f"p{p.idx}c0", speaker="narrator", voice=narrator_voice,
                         tts_text=build_tts_text(CODE_SPOKEN, "", None), char_start=0, char_end=len(p.text))]
    if p.kind == "math":
        return [TtsChunk(id=f"p{p.idx}c0", speaker="narrator", voice=narrator_voice,
                         tts_text=equation_tts_text(p), char_start=0, char_end=len(p.text))]
    chars = {c.id: c for c in book.characters}
    segments = p.segments or [Segment(speaker="narrator", text=p.text)]
    apply_styles(segments)  # rule-based whispering/shouting; keeps a style the enrich LLM already set
    chunks: list[TtsChunk] = []
    cursor = 0
    pause = lead_pause(p, book, index) if expressive else None
    sing_at = sing_offset(p, book, index) if expressive else None
    for si, seg in enumerate(segments):
        seg_text = seg.text.strip()
        if not seg_text:
            continue
        base = p.text.find(seg_text, cursor)
        if base < 0:  # segment does not match the paragraph; callers validate, so degrade to whole paragraph
            raise ValueError(f"paragraph {p.idx}: segment text not found: {seg_text[:40]!r}")
        cursor = base + len(seg_text)
        if seg.speaker in chars:
            voice, tags = chars[seg.speaker].voice, chars[seg.speaker].voice_tags
        else:
            voice, tags = narrator_voice, ""
        is_char = seg.speaker in chars
        extra = ({"tag": speech_tag(segments, si), "expressive": detect_expressive(segments, si)}
                 if expressive and is_char else {})
        parts = [(base, seg_text)]
        if sing_at is not None and base < sing_at < cursor:  # the song starts inside this segment (after 'sing:')
            parts = [(base, p.text[base:sing_at].rstrip()), (sing_at, p.text[sing_at:cursor])]
        for part_base, part_text in parts:
            sung = sing_at is not None and part_base >= sing_at
            for s, e in _groups(p, part_text, part_base):
                spoken = spoken_text(p, s, e)
                if not expressive:
                    text = _legacy_tts_text(spoken, tags, seg.emotion, seg.style, is_char)
                elif sung:  # singing replaces whisper/shout; no sfx or expressive_* inside a song
                    text = build_tts_text(spoken, tags, seg.emotion, "singing", pause=pause)
                else:
                    text = build_tts_text(spoken, tags, seg.emotion, seg.style, sfx=is_char, pause=pause, **extra)
                pause = None  # only the paragraph's first chunk
                chunks.append(TtsChunk(
                    id=f"p{p.idx}c{len(chunks)}", speaker=seg.speaker if is_char else "narrator",
                    voice=voice, tts_text=text, char_start=s, char_end=e))
    return chunks


def rechunk_book(book: Book) -> None:
    """Rebuild every paragraph's tts_chunks with the current rules and stamp CHUNK_RULES_VERSION. Keeps segment
    styles (apply_styles only fills in missing ones). Raises ValueError if a segment does not match its paragraph."""
    index = BookIndex(book)   # fresh: segments / sections may have changed since any cached one
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book, index=index)
    book.chunk_rules = CHUNK_RULES_VERSION


def needs_rechunk(book: Book) -> bool:
    return (book.chunk_rules or 0) < CHUNK_RULES_VERSION


def _legacy_tts_text(spoken: str, voice_tags: str, emotion: str | None, style: str | None, sfx: bool) -> str:
    """tts_text as built before #73 (laughter was the only sfx). Only for estimating re-render cost."""
    from .delivery import with_laughter
    tags = voice_tags + (f"<|style:{style}|>" if style else "") + (f"<|emotion:{emotion}|>" if emotion else "")
    text = normalize_for_tts(spoken)
    return tags + (with_laughter(text) if sfx else text)
