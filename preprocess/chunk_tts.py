"""chunk_tts (no LLM): split each paragraph's segments into TTS chunks <= 280 chars on sentence boundaries.

Usage: uv run --project backend python preprocess/chunk_tts.py --book scandal-in-bohemia
Idempotent: always recomputes tts_chunks from paragraph text, segments, characters and narrator.

Each chunk records char_start/char_end offsets into paragraph.text (for highlighting) and a tts_text made of
[voice tags][emotion tag] + lightly normalized spoken text. Timestamps disable Boson's own text normalization,
so numbers are expanded here.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.schema import Book, Paragraph, Segment, TtsChunk  # noqa: E402
from app.textnorm import normalize_for_tts  # noqa: E402,F401

MAX_CHARS = 280
DEFAULT_VOICE = "default"
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "messrs", "capt", "col", "prof", "no", "vs", "etc", "sr", "jr", "gen", "lt"}

_SENT_END = re.compile(r"""[.!?]+["')\]]*(?=\s+)""")


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) offsets of sentences; spans cover all non-whitespace text."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        before = text[start:m.start() + 1]
        last_word = re.findall(r"[A-Za-z]+\.?$", before)
        word = last_word[0].rstrip(".").lower() if last_word else ""
        nxt = text[end:].lstrip()
        if m.group().rstrip("\"')]") == ".":  # a lone period, not "?!" or an ellipsis
            if word in ABBREVIATIONS or re.search(r"(?:^|\s)[A-Z]\.$", before):
                continue  # "Mr.", initials such as "J. Smith"
        if nxt[:1].islower():
            continue  # ellipsis, dialogue tag ('"Well?" said he.'), or other lowercase continuation
        spans.append((start, end))
        start = end + (len(text[end:]) - len(text[end:].lstrip()))
    if start < len(text) and text[start:].strip():
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    args = ap.parse_args()
    path = ROOT / "data/books" / args.book / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    total = 0
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
        total += sum(len(c.tts_text) for c in p.tts_chunks)
    path.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    n = sum(len(p.tts_chunks) for p in book.paragraphs)
    print(f"{args.book}: {n} chunks, {total} TTS chars (~${total / 1000 * 0.015:.2f} at $0.015/1K)")


if __name__ == "__main__":
    main()
