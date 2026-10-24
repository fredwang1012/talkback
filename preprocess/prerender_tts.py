"""prerender_tts: synthesise every TTS chunk of a book into the disk cache so reading (and the demo) needs no live calls.

Usage: uv run --project backend python preprocess/prerender_tts.py --book scandal-in-bohemia [--yes] [--limit N]
Spends Boson credit ($0.015 per 1K characters). Prints the estimate first and asks for confirmation above $5.
Cache hits are free, so it is safe to rerun after an interruption. Concurrency 4, with the client's backoff on rate limits.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import tts_cache  # noqa: E402
from app.boson import BosonClient, BosonError  # noqa: E402
from app.schema import Book  # noqa: E402

RATE_PER_1K_CHARS = 0.015
CONFIRM_ABOVE_USD = 5.0
CONCURRENCY = 4


def pending_work(book: Book) -> list[tuple[str, str, str, int]]:
    """(tts_text, voice, plain_text, chars) for every chunk that is not cached yet."""
    out = []
    for p in book.paragraphs:
        for c in p.tts_chunks:
            if not tts_cache.is_cached(c.tts_text, c.voice):
                out.append((c.tts_text, c.voice, p.text[c.char_start:c.char_end], len(c.tts_text)))
    return out


def estimate_usd(work: list[tuple[str, str, str, int]]) -> float:
    return sum(w[3] for w in work) / 1000 * RATE_PER_1K_CHARS


async def run(book: Book, limit: int | None, yes: bool) -> int:
    work = pending_work(book)
    total = sum(1 for p in book.paragraphs for _ in p.tts_chunks)
    if limit is not None:  # --limit 0 means render nothing (a dry run), never everything
        work = work[:limit]
    est = estimate_usd(work)
    print(f"{book.id}: {len(work)} of {total} chunks to render ({sum(w[3] for w in work)} chars), estimated ${est:.2f}")
    if not work:
        return 0
    if est > CONFIRM_ABOVE_USD and not yes:
        sys.exit(f"Estimate ${est:.2f} exceeds ${CONFIRM_ABOVE_USD:.0f}; rerun with --yes to proceed.")

    try:
        from app.config import settings
        settings.require_boson_key()
    except RuntimeError as e:
        sys.exit(str(e))
    client = BosonClient()
    sem = asyncio.Semaphore(CONCURRENCY)
    done = failed = 0

    async def one(w):
        nonlocal done, failed
        async with sem:
            try:
                await tts_cache.get_or_create(client, w[0], w[1], w[2])
                done += 1
            except BosonError as e:
                failed += 1
                if e.type == "insufficient_quota":  # not transient: stop everything
                    sys.exit(f"Boson billing refused the request ({e.code}). Fix billing, then rerun; finished chunks are cached.")
                print(f"  failed: {e}")
            if (done + failed) % 25 == 0:
                print(f"  {done + failed}/{len(work)}")

    await asyncio.gather(*(one(w) for w in work))
    print(f"rendered {done}, failed {failed}")
    return failed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    b = Book.model_validate_json((ROOT / "data/books" / a.book / "book.json").read_text(encoding="utf-8"))
    sys.exit(1 if asyncio.run(run(b, a.limit, a.yes)) else 0)
