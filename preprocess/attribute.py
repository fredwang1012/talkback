"""attribute (no LLM): give untagged dialogue to the right character with deterministic heuristics.

Usage: uv run --project backend python preprocess/attribute.py --book hp1-sorcerers-stone [--dry] [--examples 10]
                                                               [--root DATA_ROOT] [--estimate-llm]
Without --dry the book.json is rewritten (a book.json.bak is kept once) and tts_chunks are recomputed.
--estimate-llm only prints what an LLM pass over the still-ambiguous lines would cost; it never calls a model.
See backend/app/enrich/attribute.py for the rules.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.books import save_book  # noqa: E402
from app.enrich import attribute as at  # noqa: E402
from app.schema import Book  # noqa: E402
from app.ttschunks import rechunk_book  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--examples", type=int, default=0)
    ap.add_argument("--root", default=str(ROOT / "data/books"))
    ap.add_argument("--estimate-llm", action="store_true")
    args = ap.parse_args()
    estimates, total_new = [], 0
    for bid in args.book:
        path = Path(args.root) / bid / "book.json"
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        ds = at.attribute_book(book, apply=not args.dry)
        total_new += sum(1 for d in ds if d.speaker)
        print(f"{bid}: {at.summarize(ds)}")
        estimates.append((book, [d for d in ds if d.confidence == "ambiguous"]))
        for d in [d for d in ds if d.speaker][:args.examples]:
            print(f"  [{d.para}] {d.speaker} ({d.rule}/{d.confidence}): {d.text[:90]}")
        if not args.dry and any(d.speaker for d in ds):
            bak = path.with_name(path.name + ".bak")
            if not bak.exists():
                shutil.copy2(path, bak)
            rechunk_book(book)  # stamps Book.chunk_rules
            save_book(path, book)
    if args.estimate_llm:
        e = at.llm_estimate(estimates)
        n = sum(len(ds) for _, ds in estimates)
        print(f"LLM pass (Gemini, NOT run): {n} ambiguous lines, {e['calls']} calls, ~{e['in_tokens']} in / "
              f"~{e['out_tokens']} out tokens, estimated ${e['usd']:.2f}")


if __name__ == "__main__":
    main()
