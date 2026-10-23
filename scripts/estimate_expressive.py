"""Estimate what the #73 expressive tags (sfx, pauses, expressive_*) would cost to re-voice. Offline and free.

Run from the repo root: uv run --project backend python scripts/estimate_expressive.py [--books-dir data/books]

For each book.json it rebuilds every paragraph's chunks twice in memory, without and with the #73 rules
(ttschunks.chunk_paragraph(..., expressive=False/True)), and counts the chunks whose tts_text changes. A changed
tts_text is a new TTS cache key, so such a chunk is re-rendered by Boson the next time it is read ($0.015 per 1K
characters of tts_text). Nothing is written: book.json and the audio cache are left as they are.

--list-sung prints, per book, every paragraph the #74 singing rule would sing (idx and its first 80 sung characters),
for review before any chunks are rebuilt.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.schema import Book  # noqa: E402
from app.ttschunks import chunk_paragraph, sing_offset  # noqa: E402

PER_1K = 0.015
MODEL, FORMAT = "higgs-tts-3", "mp3"  # mirrors app.tts_cache.cache_key (not imported: it pulls in settings/.env)


def cache_key(tts_text: str, voice: str) -> str:
    return hashlib.sha256(json.dumps([MODEL, tts_text, voice, FORMAT, True], ensure_ascii=False).encode()).hexdigest()


def estimate(path: Path, tts_dir: Path) -> dict:
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    out = {"chunks": 0, "changed": 0, "chars": 0, "voiced": 0, "voiced_chars": 0, "kinds": {}}
    for p in book.paragraphs:
        old = chunk_paragraph(p, book, expressive=False)
        new = chunk_paragraph(p, book)
        out["chunks"] += len(new)
        for a, b in zip(old, new):
            if a.tts_text == b.tts_text:
                continue
            out["changed"] += 1
            out["chars"] += len(b.tts_text)
            for kind in ("style:singing", "sfx:", "long_pause", "prosody:pause", "expressive_high", "expressive_low"):
                if b.tts_text.count(kind) > a.tts_text.count(kind):
                    out["kinds"][kind] = out["kinds"].get(kind, 0) + 1
            if (tts_dir / f"{cache_key(a.tts_text, a.voice)}.{FORMAT}").exists():
                out["voiced"] += 1
                out["voiced_chars"] += len(b.tts_text)
    return out


def list_sung(path: Path) -> list[tuple[int, str]]:
    """(paragraph idx, first 80 sung characters) for every paragraph the singing rule (#74) would sing."""
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    out = []
    for p in book.paragraphs:
        at = sing_offset(p, book)
        if at is not None:
            out.append((p.idx, p.text[at:at + 80]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books-dir", type=Path, default=ROOT / "data" / "books")
    ap.add_argument("--list-sung", action="store_true", help="only list the paragraphs that would be sung (#74)")
    args = ap.parse_args()
    if args.list_sung:
        sys.stdout.reconfigure(encoding="utf-8")
        for path in sorted(args.books_dir.glob("*/book.json")):
            try:
                rows = list_sung(path)
            except ValueError as e:  # a book.json written by a newer/older schema
                print(f"== {path.parent.name}: skipped ({str(e).splitlines()[0]})")
                continue
            print(f"== {path.parent.name}: {len(rows)} sung paragraph(s)")
            for idx, head in rows:
                print(f"  p{idx:<6} {head!r}")
        return
    tts_dir = args.books_dir.parent / "cache" / "tts"
    total = {"changed": 0, "chars": 0, "voiced": 0, "voiced_chars": 0}
    print(f"{'book':40} {'chunks':>7} {'changed':>8} {'chars':>9} {'all $':>8} {'voiced':>7} {'voiced $':>9}  new tags")
    for path in sorted(args.books_dir.glob("*/book.json")):
        r = estimate(path, tts_dir)
        for k in total:
            total[k] += r[k]
        kinds = ", ".join(f"{k.rstrip(':')} {v}" for k, v in sorted(r["kinds"].items()))
        print(f"{path.parent.name[:40]:40} {r['chunks']:7} {r['changed']:8} {r['chars']:9} "
              f"{r['chars'] / 1000 * PER_1K:8.2f} {r['voiced']:7} {r['voiced_chars'] / 1000 * PER_1K:9.2f}  {kinds}")
    print(f"{'TOTAL':40} {'':7} {total['changed']:8} {total['chars']:9} {total['chars'] / 1000 * PER_1K:8.2f} "
          f"{total['voiced']:7} {total['voiced_chars'] / 1000 * PER_1K:9.2f}")
    print("all $ = re-voicing every changed chunk (upper bound); voiced $ = changed chunks whose current audio is in the"
          f" local cache ({tts_dir}) under the book's own voice (lower bound: audio cached under a reader's cast or"
          " pitch/speed voice is not found). Applies only after a book's chunks are rebuilt (preprocess/chunk_tts.py,"
          " re-enrich); book.json and cached audio are not touched.")


if __name__ == "__main__":
    main()
