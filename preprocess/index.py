"""index: retrieval chunks (+ optional context blurbs) and multilingual-e5-small embeddings -> data/books/<id>/index/.

Usage: uv run --project backend python preprocess/index.py --book scandal-in-bohemia [--no-embed] [--blurbs]
- Default: chunks + embeddings, no LLM calls.
- --blurbs: ask Haiku 5.5 for a one-sentence context_blurb per chunk (uses the Anthropic key; off by default).
BM25 is rebuilt from chunks.json at load time, so only chunks and embeddings are stored.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.retrieval import make_chunks, save_index  # noqa: E402
from app.schema import Book  # noqa: E402


async def add_blurbs(book: Book, chunks) -> None:
    from pydantic import BaseModel, ConfigDict

    import llm

    class Blurb(BaseModel):
        model_config = ConfigDict(extra="forbid")
        blurb: str

    system = ("Write ONE sentence (max 30 words) situating the passage in the story, using only the preceding text "
              "provided. Do not mention anything that happens after the passage.")

    async def one(c):
        prefix = "\n".join(p.text for p in book.paragraphs[max(0, c.para_start - 6):c.para_start])
        user = f"Preceding text:\n{prefix or '(start of the story)'}\n\nPassage:\n{c.text}"
        c.context_blurb = (await llm.call_structured("claude-haiku-5-5", system, user, Blurb, max_tokens=200)).blurb

    await asyncio.gather(*(one(c) for c in chunks))
    llm.print_usage()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--blurbs", action="store_true")
    args = ap.parse_args()

    book = Book.model_validate_json((ROOT / "data/books" / args.book / "book.json").read_text(encoding="utf-8"))
    chunks = make_chunks(book)
    if args.blurbs:
        asyncio.run(add_blurbs(book, chunks))

    embeddings = None
    if not args.no_embed:
        from app.embedder import E5Embedder
        embedder = E5Embedder()
        embeddings = embedder.embed_passages([(c.context_blurb + "\n" + c.text).strip() for c in chunks])
    out = ROOT / "data/books" / args.book / "index"
    save_index(out, chunks, embeddings)
    print(f"{args.book}: {len(chunks)} retrieval chunks, embeddings={'yes' if embeddings is not None else 'no'} -> {out}")


if __name__ == "__main__":
    main()
