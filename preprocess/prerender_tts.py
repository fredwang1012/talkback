"""prerender_tts: synthesise every TTS chunk of a book into the disk cache so reading (and the demo) needs no live calls.

Usage: uv run --project backend python preprocess/prerender_tts.py --book hp1-sorcerers-stone [--yes] [--limit N]
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


def pending_work(book: Book, from_para: int = 0, to_para: int | None = None,
                 narrator_voice: str | None = None) -> list[tuple[str, str, str, int]]:
    """(tts_text, voice, plain_text, chars) for every chunk in the paragraph range that is not cached yet.
    narrator_voice renders the narrator's chunks in another voice (e.g. a cloned narrator picked in the reader)."""
    out = []
    for p in book.paragraphs[from_para:None if to_para is None else to_para + 1]:
        for c in p.tts_chunks:
            voice = narrator_voice if narrator_voice and c.speaker == "narrator" else c.voice
            if not tts_cache.is_cached(c.tts_text, voice):
                out.append((c.tts_text, voice, p.text[c.char_start:c.char_end], len(c.tts_text)))
    return out


def estimate_usd(work: list[tuple[str, str, str, int]]) -> float:
    return sum(w[3] for w in work) / 1000 * RATE_PER_1K_CHARS


async def run(book: Book, limit: int | None, yes: bool, from_para: int = 0, to_para: int | None = None,
              narrator_voice: str | None = None) -> int:
    work = pending_work(book, from_para, to_para, narrator_voice)
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
    ap.add_argument("--from-para", type=int, default=0)
    ap.add_argument("--to-para", type=int)
    ap.add_argument("--narrator-voice", help="render narrator chunks in this voice id (e.g. the cloned narrator)")
    a = ap.parse_args()
    b = Book.model_validate_json((ROOT / "data/books" / a.book / "book.json").read_text(encoding="utf-8"))
    sys.exit(1 if asyncio.run(run(b, a.limit, a.yes, a.from_para, a.to_para, a.narrator_voice)) else 0)
