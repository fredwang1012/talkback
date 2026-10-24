"""chunk_tts (no LLM): split each paragraph's segments into TTS chunks <= 280 chars on sentence boundaries.

Usage: uv run --project backend python preprocess/chunk_tts.py --book hp1-sorcerers-stone [--redetect-styles]
Idempotent: always recomputes tts_chunks from paragraph text, segments, characters and narrator, and stamps
Book.chunk_rules with ttschunks.CHUNK_RULES_VERSION. The server does the same by itself for out-of-date books
(books.load_book), so this is only needed to rebuild a book.json offline, e.g. before committing it.

Segment styles (whispering/shouting) are kept: some come from the enrich LLM and the rules cannot find them again.
--redetect-styles clears every style first and re-detects it from the rules only (the old behaviour; right for
books whose styles all came from the rules, wrong for LLM-enriched ones such as Alice).

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
    ABBREVIATIONS, DEFAULT_VOICE, MAX_CHARS, build_tts_text, chunk_paragraph, rechunk_book, sentence_spans, split_long,
)


def rechunk_file(path: Path, redetect_styles: bool = False) -> Book:
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    if redetect_styles:
        for p in book.paragraphs:
            for seg in p.segments:
                seg.style = None
    rechunk_book(book)
    save_book(path, book)
    return book


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    ap.add_argument("--redetect-styles", action="store_true",
                    help="clear segment styles and re-detect them from the rules (drops LLM-assigned styles)")
    args = ap.parse_args()
    for book_id in args.book:
        book = rechunk_file(ROOT / "data/books" / book_id / "book.json", args.redetect_styles)
        n = sum(len(p.tts_chunks) for p in book.paragraphs)
        total = sum(len(c.tts_text) for p in book.paragraphs for c in p.tts_chunks)
        print(f"{book_id}: {n} chunks, {total} TTS chars (~${total / 1000 * 0.015:.2f} at $0.015/1K)")


if __name__ == "__main__":
    main()
