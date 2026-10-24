"""minor_voices (no LLM, free): give dialogue by speakers outside the cast one of two generic voices (issue #70).

Usage: uv run --project backend python preprocess/minor_voices.py --book hp1-sorcerers-stone [--book ...] [--dry]
                                                                  [--examples 8] [--root DATA_ROOT]
Without --dry book.json is rewritten (book.json.bak kept once) and tts_chunks recomputed. Voices and the gender rules
live in backend/app/enrich/minor.py (MINOR_VOICES). Data under data/books/hp* is gitignored: never commit it.
"""
import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.books import save_book  # noqa: E402
from app.enrich import minor  # noqa: E402
from app.schema import Book  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--examples", type=int, default=0)
    ap.add_argument("--root", default=str(ROOT / "data/books"))
    args = ap.parse_args()
    total = Counter()
    for bid in args.book:
        path = Path(args.root) / bid / "book.json"
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        lines = minor.assign_minor_voices(book, apply=not args.dry)
        by = Counter(d.speaker for d in lines)
        total.update(by)
        print(f"{bid}: {len(lines)} lines -> {dict(by)}")
        for d in lines[:args.examples]:
            print(f"  [{d.para}] {d.speaker}: {d.text[:70]} || {d.context[-60:]}")
        if not args.dry and lines:
            bak = path.with_name(path.name + ".bak")
            if not bak.exists():
                shutil.copy2(path, bak)
            save_book(path, book)
    print(f"TOTAL {sum(total.values())}: {dict(total)}" + (" (dry run, nothing written)" if args.dry else ""))


if __name__ == "__main__":
    main()
