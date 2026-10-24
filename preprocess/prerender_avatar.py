"""prerender_avatar (M9, stretch): talking-head clips for a stretch of the story, driven by the already-cached TTS audio.

Usage: uv run --project backend python preprocess/prerender_avatar.py --book scandal-in-bohemia --from-para 22 --to-para 40 [--yes]
Needs: portraits at data/portraits/<book>/<speaker>.png|jpg|webp (original art only), pre-rendered TTS audio
(run prerender_tts.py first) and the Boson key. Chunks of speakers without a portrait are skipped.
Output: data/cache/avatar/<book>/<chunk_id>.mp4, served by /api/avatar/<chunk_id>?book=<book>.
Cost is about $0.08 per minute of speech; the estimate assumes ~15 characters per second. Confirms above $5.
Run scripts/probe_avatar.py first: the request shape and whether the MP4 carries audio are not verified yet.
"""
import argparse
import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import tts_cache  # noqa: E402
from app.boson import BosonClient, BosonError  # noqa: E402
from app.config import settings  # noqa: E402
from app.schema import Book  # noqa: E402

USD_PER_MINUTE = 0.09
CHARS_PER_SECOND = 15
CONFIRM_ABOVE_USD = 5.0
CONCURRENCY = 2  # avatar replicas are scarce; the client also honours all_replicas_busy
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def portrait_file(book_id: str, speaker: str) -> Path | None:
    for ext in MIME:
        p = settings.data_dir / "portraits" / book_id / f"{speaker}.{ext}"
        if p.is_file():
            return p
    return None


def plan(book: Book, from_para: int, to_para: int) -> list[dict]:
    """Chunks in range that have a portrait, cached audio and no clip yet."""
    out = []
    for p in book.paragraphs[from_para:to_para + 1]:
        for c in p.tts_chunks:
            portrait = portrait_file(book.id, c.speaker)
            audio = tts_cache._paths(tts_cache.cache_key(c.tts_text, c.voice))[0]
            clip = settings.data_dir / "cache" / "avatar" / book.id / f"{c.id}.mp4"
            if portrait and audio.is_file() and not clip.exists():
                out.append({"chunk": c, "portrait": portrait, "audio": audio, "clip": clip,
                            "seconds": max(1.0, (c.char_end - c.char_start) / CHARS_PER_SECOND)})
    return out


def estimate_usd(work: list[dict]) -> float:
    return sum(w["seconds"] for w in work) / 60 * USD_PER_MINUTE


async def render(client: BosonClient, w: dict) -> None:
    ref = base64.b64encode(w["portrait"].read_bytes()).decode()
    audio = base64.b64encode(w["audio"].read_bytes()).decode()
    vid = await client.create_video(ref, audio, image_mime=MIME[w["portrait"].suffix.lstrip(".").lower()])
    await client.wait_video(vid)
    w["clip"].parent.mkdir(parents=True, exist_ok=True)
    w["clip"].write_bytes(await client.download_video(vid))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--from-para", type=int, required=True)
    ap.add_argument("--to-para", type=int, required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    book = Book.model_validate_json((ROOT / "data/books" / args.book / "book.json").read_text(encoding="utf-8"))
    work = plan(book, args.from_para, args.to_para)
    est = estimate_usd(work)
    print(f"{book.id} ¶{args.from_para}-{args.to_para}: {len(work)} clips to render, estimated ${est:.2f}")
    if not work:
        print("Nothing to do: needs portraits in data/portraits/<book>/, cached TTS audio, and clips not already rendered.")
        return
    if est > CONFIRM_ABOVE_USD and not args.yes:
        sys.exit(f"Estimate ${est:.2f} exceeds ${CONFIRM_ABOVE_USD:.0f}; rerun with --yes.")
    settings.require_boson_key()
    client, sem, failed = BosonClient(), asyncio.Semaphore(CONCURRENCY), 0

    async def one(w: dict) -> None:
        nonlocal failed
        async with sem:
            try:
                await render(client, w)
            except BosonError as e:
                failed += 1
                if e.type == "insufficient_quota":
                    sys.exit(f"Boson billing refused the request ({e.code}). Finished clips are kept.")
                print(f"  failed {w['chunk'].id}: {e}")

    await asyncio.gather(*(one(w) for w in work))
    print(f"rendered {len(work) - failed}, failed {failed}")


if __name__ == "__main__":
    asyncio.run(main())
