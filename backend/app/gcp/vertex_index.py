"""Retrieval index backed by Vertex AI Vector Search 2.0: vector + text search fused with RRF.

Spoiler invariant (novels): the query filter AND a post-filter here both enforce para_end <= reader_para.
The store never post-filters, so this class is the guarantee.
"""
import asyncio

from ..retrieval import Chunk
from .vectorstore import rrf, spoiler_filter


class VertexIndex:
    def __init__(self, book, embedder, store):
        self.book, self.embedder, self.store = book, embedder, store

    @property
    def mode(self) -> str:
        return "vertex"

    async def asearch(self, query: str, reader_para: int, k: int = 4, *, spoiler_safe: bool = True) -> list[Chunk]:
        """Top-k chunks in book order. With spoiler_safe, only chunks with para_end <= reader_para are returned."""
        if not query.strip():
            return []
        flt = spoiler_filter(self.book.id, reader_para if spoiler_safe else None)
        vec = await asyncio.to_thread(self.embedder.embed_query, query)
        vec = [float(x) for x in vec]
        n = 4 * k
        vec_hits, text_hits = await asyncio.gather(
            self.store.vector_search(vec, flt, n), self.store.text_search(query, flt, n))
        fused = rrf([vec_hits, text_hits])
        chunks = []
        for r in fused:
            d = r["data"]
            if d.get("book_id") != self.book.id:
                continue
            if spoiler_safe and not (isinstance(d.get("para_end"), int) and d["para_end"] <= reader_para):
                continue  # post-filter: never trust the store
            chunks.append(Chunk(id=d["chunk_id"], para_start=int(d["para_start"]),
                                para_end=int(d["para_end"]), text=d["text"]))
            if len(chunks) == k:
                break
        return sorted(chunks, key=lambda c: c.para_start)
