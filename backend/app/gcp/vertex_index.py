"""Retrieval index backed by Vertex AI Vector Search 2.0: vector + text search fused with RRF.

Spoiler invariant (novels): the query filter AND a post-filter here both enforce para_end <= reader_para for the
book being read; earlier books of the same series (`earlier`, id -> title) are eligible in full.
The store never post-filters, so this class is the guarantee.
Every hit must also belong to this book (or a listed earlier book) in the query filter and the post-filter, so
datapoints a delete could not remove (app/gcp/orphans.py) are never returned: book ids are never reused.
"""
import asyncio

from ..retrieval import Chunk
from .vectorstore import rrf, spoiler_filter


class VertexIndex:
    def __init__(self, book, embedder, store, earlier: dict[str, str] | None = None):
        self.book, self.embedder, self.store = book, embedder, store
        self.earlier = dict(earlier or {})

    @property
    def mode(self) -> str:
        return "vertex"

    def _eligible(self, d: dict, reader_para: int, spoiler_safe: bool) -> bool:
        if d.get("book_id") in self.earlier:
            return self.book.kind == "novel"  # earlier books take part in all novel talk (spoiler-safe or full-book)
        if d.get("book_id") != self.book.id:
            return False
        return not spoiler_safe or (isinstance(d.get("para_end"), int) and d["para_end"] <= reader_para)

    async def asearch(self, query: str, reader_para: int, k: int = 8, *, spoiler_safe: bool = True) -> list[Chunk]:
        """Top-k chunks: earlier-book chunks first, then this book's in book order."""
        if not query.strip():
            return []
        flt = spoiler_filter(self.book.id, reader_para if spoiler_safe else None,
                             list(self.earlier) if self.book.kind == "novel" else None)
        n = 4 * k
        text_task = asyncio.ensure_future(self.store.text_search(query, flt, n))  # needs no embedding: start now
        try:
            vec = await asyncio.to_thread(self.embedder.embed_query, query)
            vec_hits = await self.store.vector_search([float(x) for x in vec], flt, n)
        except BaseException:
            text_task.cancel()
            raise
        text_hits = await text_task
        fused = rrf([_uid(vec_hits), _uid(text_hits)], key="_uid")
        chunks = []
        for r in fused:
            d = r["data"]
            if not self._eligible(d, reader_para, spoiler_safe):
                continue  # post-filter: never trust the store
            other = d["book_id"] != self.book.id
            chunks.append(Chunk(id=d["chunk_id"], para_start=int(d["para_start"]), para_end=int(d["para_end"]),
                                text=d["text"], book_id=d["book_id"] if other else "",
                                book_title=self.earlier.get(d["book_id"], "") if other else ""))
            if len(chunks) == k:
                break
        return sorted(chunks, key=lambda c: (not c.book_id, c.para_start))


def _uid(hits: list[dict]) -> list[dict]:
    """Chunk ids repeat across books ("r0" in every book): fuse on book + chunk."""
    for h in hits:
        h["data"]["_uid"] = f"{h['data'].get('book_id')}__{h['data'].get('chunk_id')}"
    return hits
