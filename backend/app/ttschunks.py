"""Split paragraphs into Boson TTS chunks (<= 280 chars, sentence-aligned). Shared by preprocess/chunk_tts.py and uploads."""
import re

from .delivery import apply_styles, with_laughter
from .schema import Book, Paragraph, Segment, TtsChunk
from .textnorm import normalize_for_tts

MAX_CHARS = 280
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
                   sfx: bool = False) -> str:
    """[voice prosody tags][style][emotion][spoken text, with a laughter sfx before laughter onomatopoeia if sfx]."""
    tags = voice_tags + (f"<|style:{style}|>" if style else "") + (f"<|emotion:{emotion}|>" if emotion else "")
    text = normalize_for_tts(spoken)
    return tags + (with_laughter(text) if sfx else text)


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


def chunk_paragraph(p: Paragraph, book: Book) -> list[TtsChunk]:
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
    for seg in segments:
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
        # pack whole sentences greedily up to MAX_CHARS; over-long sentences are split first
        pieces: list[tuple[int, int]] = []
        for s, e in sentence_spans(seg_text):
            if e - s > MAX_CHARS:
                pieces += split_long(seg_text[s:e], base + s)
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
        for s, e in groups:
            spoken = spoken_text(p, s, e)
            chunks.append(TtsChunk(
                id=f"p{p.idx}c{len(chunks)}", speaker=seg.speaker if seg.speaker in chars else "narrator",
                voice=voice, tts_text=build_tts_text(spoken, tags, seg.emotion, seg.style, sfx=seg.speaker in chars),
                char_start=s, char_end=e))
    return chunks
