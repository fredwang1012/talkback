"""Index a book's retrieval chunks into Vector Search: embed with Gemini, upsert data objects."""
import asyncio
import json

from ..config import settings
from ..retrieval import Chunk, make_chunks
from ..schema import Book

UPSERT_BATCH = 500


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


async def index_book(book: Book, chunks: list[Chunk], embedder, store) -> int:
    """Embed every chunk and upsert it; returns the number of objects written."""
    if not chunks:
        return 0
    embeddings = await asyncio.to_thread(embedder.embed_documents, [embed_text(c) for c in chunks])
    objects = build_objects(book, chunks, embeddings)
    for i in range(0, len(objects), UPSERT_BATCH):
        await store.upsert(objects[i:i + UPSERT_BATCH])
    return len(objects)


def datapoint_ids(book: Book) -> list[str]:
    """Ids of the Vector Search data objects index_book writes for this book (see build_objects)."""
    return [f"{book.id}__{c.id}" for c in load_chunks(book)]
