"""Index a book's retrieval chunks into Vector Search: embed with Gemini, upsert data objects."""
import asyncio
import json

from ..config import settings
from ..retrieval import Chunk, make_chunks
from ..schema import Book

EMBED_BATCH = 100        # chunks per embedding call
EMBED_CONCURRENCY = 5    # embedding calls in flight
UPSERT_CONCURRENCY = 4   # upsert calls in flight


def load_chunks(book: Book) -> list[Chunk]:
    """Chunks from data/books/<id>/index/chunks.json when present, else freshly made."""
    path = settings.data_dir / "books" / book.id / "index" / "chunks.json"
    if path.is_file():
        return [Chunk(**c) for c in json.loads(path.read_text(encoding="utf-8"))]
    return make_chunks(book)


def embed_text(chunk: Chunk) -> str:
    return f"{chunk.context_blurb}\n{chunk.text}" if chunk.context_blurb else chunk.text


def build_objects(book: Book, chunks: list[Chunk], embeddings) -> list[dict]:
    section_index = {s.id: i for i, s in enumerate(book.sections)}
    by_idx = {p.idx: p for p in book.paragraphs}
    objects = []
    for chunk, emb in zip(chunks, embeddings):
        paras = [by_idx[i] for i in range(chunk.para_start, chunk.para_end + 1) if i in by_idx]
        first = by_idx.get(chunk.para_start)
        speakers = {seg.speaker for p in paras for seg in p.segments}
        speaker = next(iter(speakers)) if len(speakers) == 1 else "narrator"
        objects.append({
            "id": f"{book.id}__{chunk.id}",
            "data": {
                "book_id": book.id,
                "chunk_id": chunk.id,
                "section": section_index.get(first.section_id, 0) if first else 0,
                "para_start": chunk.para_start,
                "para_end": chunk.para_end,
                "speaker": speaker,
                "text": chunk.text,
            },
            "embedding": [float(x) for x in emb],
        })
    return objects


async def index_book(book: Book, chunks: list[Chunk], embedder, store, on_progress=None) -> int:
    """Embed every chunk and upsert it; returns the number of objects written.

    Batches of EMBED_BATCH chunks are embedded (in worker threads) and upserted concurrently, at most
    EMBED_CONCURRENCY / UPSERT_CONCURRENCY in flight; each batch keeps its own order. `on_progress(done, total)`
    is called (on the event loop) after each batch is stored. The first failure cancels the rest and is raised.
    """
    total = len(chunks)
    if not total:
        return 0
    embed_slots, upsert_slots = asyncio.Semaphore(EMBED_CONCURRENCY), asyncio.Semaphore(UPSERT_CONCURRENCY)
    done = 0

    async def one(batch: list[Chunk]) -> None:
        nonlocal done
        async with embed_slots:
            embeddings = await asyncio.to_thread(embedder.embed_documents, [embed_text(c) for c in batch])
        objects = build_objects(book, batch, embeddings)
        async with upsert_slots:
            await store.upsert(objects)
        done += len(batch)
        if on_progress:
            on_progress(done, total)

    tasks = [asyncio.ensure_future(one(chunks[i:i + EMBED_BATCH])) for i in range(0, total, EMBED_BATCH)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return total
