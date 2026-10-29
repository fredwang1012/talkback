"""Retrieval indexes: the Vertex embedder and store, the local e5 model and the per-book Index cache."""
import threading
from functools import lru_cache

from . import books, ingest_upload, retrieval
from .config import settings


@lru_cache(maxsize=1)
def _vertex_deps():
    """Shared embedder + vector store, built on first use (no network at import or construction of the index)."""
    from .gcp.embed import GeminiEmbedder
    from .gcp.vectorstore import VectorStore
    return GeminiEmbedder(), VectorStore(settings.vs_collection_path)


def _vertex_index_ready(book_id: str) -> bool:
    """False while a book's Vertex index is building, failed or was interrupted (retrieval then uses local BM25).
    Books with no index progress recorded (seeded or indexed offline) are served from Vertex."""
    idx = (ingest_upload.read_status(book_id) or {}).get("index")
    return not idx or idx.get("state") == "done"


@lru_cache(maxsize=8)
def _index_for(book_id: str) -> retrieval.Index:
    book = books.load_book(book_id)
    if settings.retrieval_backend == "vertex" and _vertex_index_ready(book_id):
        from .gcp.vertex_index import VertexIndex
        earlier = {b.id: b.title for b in books.earlier_in_series(book)}
        return VertexIndex(book, _LazyEmbedder(), _LazyStore(), earlier)
    index_dir = settings.data_dir / "books" / book_id / "index"
    if settings.retrieval_backend == "vertex":  # Vertex index still building (or failed): local BM25, same eligibility
        if (index_dir / "chunks.json").exists():
            return retrieval.Index.load(index_dir)
        return retrieval.Index(retrieval.make_chunks(book))
    if (index_dir / "chunks.json").exists():
        return retrieval.Index.load(index_dir, embedder=_embedder())
    return retrieval.Index(retrieval.make_chunks(book))  # BM25-only until the index is built


class _LazyEmbedder:
    def embed_query(self, text: str):
        return _vertex_deps()[0].embed_query(text)


class _LazyStore:
    async def vector_search(self, *a, **kw):
        return await _vertex_deps()[1].vector_search(*a, **kw)

    async def text_search(self, *a, **kw):
        return await _vertex_deps()[1].text_search(*a, **kw)


_embedder_lock = threading.Lock()


def _embedder():
    """The shared local e5 model, loaded once (thread-safe: startup warm-up and the embedding job may both ask)."""
    with _embedder_lock:
        return _load_embedder()


@lru_cache(maxsize=1)
def _load_embedder():
    try:
        from .embedder import E5Embedder
        return E5Embedder()
    except Exception:  # sentence-transformers not installed: fall back to BM25
        return None
