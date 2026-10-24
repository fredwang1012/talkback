"""Embed a book's retrieval chunks with Gemini and upsert them into Vector Search.

Usage: uv run --project backend python scripts/index_vertex.py --book ID [--dry-run]
--dry-run only counts chunks and estimates cost; it makes no network calls.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.books import load_book  # noqa: E402
from app.config import settings  # noqa: E402
from app.gcp.indexing import embed_text, index_book, load_chunks  # noqa: E402

USD_PER_1K_CHARS = 0.000025


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    book = load_book(args.book)
    if book is None:
        sys.exit(f"book not found: {args.book}")
    chunks = load_chunks(book)
    chars = sum(len(embed_text(c)) for c in chunks)
    print(f"{book.id}: {len(chunks)} chunks, {chars} characters")
    print(f"estimated embedding cost: ${chars / 1000 * USD_PER_1K_CHARS:.4f} (estimate)")
    if args.dry_run:
        return

    from app.gcp.embed import GeminiEmbedder
    from app.gcp.vectorstore import VectorStore

    n = await index_book(book, chunks, GeminiEmbedder(), VectorStore(settings.vs_collection_path))
    print(f"upserted {n} objects")


if __name__ == "__main__":
    asyncio.run(main())
