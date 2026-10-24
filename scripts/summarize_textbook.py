"""Write the recap study notes of an existing textbook (one per chapter and numbered subsection), like an upload does.

Usage (from the repo root):
  backend/.venv/Scripts/python.exe scripts/summarize_textbook.py --book u-introduction-to-information-retrieval-95955368
  backend/.venv/Scripts/python.exe scripts/summarize_textbook.py --book <id> --yes     # really spend

Without --yes it only prints the estimate. It uses the upload caps (UPLOAD_BOOK_CAP_USD per book,
UPLOAD_TOTAL_CAP_USD cumulative in data/usage/upload_spend.json) and the upload LLM (UPLOAD_LLM, Gemini by default):
a book over a cap is refused even with --yes, and a running job stops when it passes one. Existing notes are replaced.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings  # noqa: E402
from app.enrich import pipeline, spend  # noqa: E402
from app.enrich import textbook as tb  # noqa: E402
from app.schema import Book  # noqa: E402

Run = Callable[..., Awaitable[bool]]


def main(argv: list[str] | None = None, run: Run | None = None, out: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", required=True, help="book id under data/books/")
    ap.add_argument("--yes", action="store_true", help="really run (spends money); without it only the estimate")
    args = ap.parse_args(argv)

    path = settings.data_dir / "books" / args.book / "book.json"
    try:
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError:
        out(f"No book at {path}.")
        return 2
    except ValueError as e:
        out(f"{path} does not match this branch's schema ({type(e).__name__}); run it from a branch that reads it.")
        return 2
    if book.kind != "textbook":
        out(f"{args.book} is a {book.kind}; novels get their summaries from the upload enrichment instead.")
        return 2

    est, subs = tb.plan(book)
    ts = tb.targets(book, subs)
    chapters = sum(1 for t in ts if t.level == 1)
    chars = sum(len(tb.para_text(p)) for p in book.paragraphs)
    out(f"{book.title}: {len(book.paragraphs)} paragraphs, {chars:,} characters")
    out(f"Notes: {chapters} chapters" + (f" + {len(ts) - chapters} subsections" if subs else " (subsections left out: over the cap)"))
    out(f"Estimate: ${est:.2f} (provider {settings.upload_llm}, fast tier, with margin)")
    out(f"Caps: ${settings.upload_book_cap_usd:.2f} per book; ${spend.total_usd():.2f} of ${settings.upload_total_cap_usd:.2f} total used")
    if tb.has_notes(book):
        out(f"It already has {sum(1 for s in book.summaries if s.section_id)} notes; they will be replaced.")
    reason = pipeline.check_caps(est, textbook=True)
    if reason:
        out(f"Refused: {reason}")
        return 3
    if not args.yes:
        out(f"Nothing spent. Run again with --yes to write the notes (about ${est:.2f}).")
        return 0

    before = spend.total_usd()
    last = {"msg": ""}

    def report(**f) -> None:
        msg = f"{f.get('status')}: {f.get('message', '')}"
        if f.get("progress") is not None:
            msg += f" ({float(f['progress']):.0%})"
        if msg != last["msg"]:
            last["msg"] = msg
            out(msg)

    ok = asyncio.run((run or pipeline.enrich_textbook)(args.book, report))
    out(f"Spent about ${spend.total_usd() - before:.4f}." if ok else "Stopped; notes written so far were kept.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
