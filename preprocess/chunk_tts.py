"""chunk_tts (no LLM): split each paragraph's segments into TTS chunks <= 280 chars on sentence boundaries.

Usage: uv run --project backend python preprocess/chunk_tts.py --book hp1-sorcerers-stone
Idempotent: always recomputes tts_chunks from paragraph text, segments, characters and narrator.

Each chunk records char_start/char_end offsets into paragraph.text (for highlighting) and a tts_text made of
[voice tags][style tag][emotion tag] + lightly normalized spoken text. Timestamps disable Boson's own text normalization,
so numbers are expanded here.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.schema import Book  # noqa: E402
from app.books import save_book  # noqa: E402
from app.textnorm import normalize_for_tts  # noqa: E402,F401
from app.ttschunks import (  # noqa: E402,F401
    ABBREVIATIONS, DEFAULT_VOICE, MAX_CHARS, build_tts_text, chunk_paragraph, sentence_spans, split_long,
)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    args = ap.parse_args()
    for book_id in args.book:
        path = ROOT / "data/books" / book_id / "book.json"
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        total = 0
        for p in book.paragraphs:
            for seg in p.segments:
                seg.style = None  # re-detect from the current rules (HP books carry no LLM styles)
            p.tts_chunks = chunk_paragraph(p, book)
            total += sum(len(c.tts_text) for c in p.tts_chunks)
        save_book(path, book)
        n = sum(len(p.tts_chunks) for p in book.paragraphs)
        print(f"{book_id}: {n} chunks, {total} TTS chars (~${total / 1000 * 0.015:.2f} at $0.015/1K)")


if __name__ == "__main__":
    main()
