"""Split paragraphs into Boson TTS chunks (<= 280 chars, sentence-aligned). Shared by preprocess/chunk_tts.py and uploads."""
import re

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


def build_tts_text(spoken: str, voice_tags: str, emotion: str | None) -> str:
    tags = voice_tags + (f"<|emotion:{emotion}|>" if emotion else "")
    return tags + normalize_for_tts(spoken)


def chunk_paragraph(p: Paragraph, book: Book) -> list[TtsChunk]:
    narrator_voice = book.narrator.voice if book.narrator else DEFAULT_VOICE
    chars = {c.id: c for c in book.characters}
    segments = p.segments or [Segment(speaker="narrator", text=p.text)]
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
        groups: list[tuple[int, int]] = []
        for s, e in pieces:
            if groups and e - groups[-1][0] <= MAX_CHARS:
                groups[-1] = (groups[-1][0], e)
            else:
                groups.append((s, e))
        for s, e in groups:
            spoken = p.text[s:e]
            chunks.append(TtsChunk(
                id=f"p{p.idx}c{len(chunks)}", speaker=seg.speaker if seg.speaker in chars else "narrator",
                voice=voice, tts_text=build_tts_text(spoken, tags, seg.emotion),
                char_start=s, char_end=e))
    return chunks
