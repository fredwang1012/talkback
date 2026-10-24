"""Bake main characters' voices into custom Boson voices so read mode and Talkback sound the same (issue #69).

Default: print the cost estimate for every book and exit (no network). Paid run needs --run:
    uv run --project backend python scripts/bake_voices.py                    # estimate only, all books
    uv run --project backend python scripts/bake_voices.py --run --book hp1-sorcerers-stone [--character dobby]
Cost: TTS $0.015 per 1K characters of sample text; creating a voice is unpriced. A book.json.bak is written first.
"""
import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import books, voice_bake  # noqa: E402
from app.boson import BosonClient  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="really call Boson (paid); without it only print the estimate")
    ap.add_argument("--book", action="append", help="book id (repeatable); default all")
    ap.add_argument("--character", action="append", help="character id (repeatable); default all main characters")
    a = ap.parse_args()
    ids = a.book or [p.parent.name for p in sorted(Path(settings.data_dir, "books").glob("*/book.json"))]
    loaded = [b for b in (books.load_book(i) for i in ids) if b]
    est = voice_bake.estimate([b.model_copy(deep=True) for b in loaded])
    total_chars = total_voices = 0
    for e in est:
        print(f"{e.book_id}: {len(e.characters)} voices, {e.chars} chars, ${e.usd:.4f}  {', '.join(e.characters)}")
        total_chars += e.chars
        total_voices += len(e.characters)
    print(f"TOTAL: {total_voices} voices, {total_chars} TTS chars = ${total_chars / 1000 * voice_bake.TTS_USD_PER_1K_CHARS:.4f} "
          "(+ voice creation, unpriced)")
    if not a.run:
        print("Estimate only. Re-run with --run to render and register.")
        return
    client = BosonClient()
    only = set(a.character) if a.character else None
    for b in loaded:
        path = Path(settings.data_dir, "books", b.id, "book.json")
        book = b.model_copy(deep=True)
        done = asyncio.run(voice_bake.bake_book(client, book, only))
        if done:
            shutil.copy2(path, path.with_suffix(".json.bak"))
            books.save_book(path, book)
        print(f"{b.id}: baked {done}")


if __name__ == "__main__":
    main()
