import asyncio

import numpy as np
import pytest

from app import main, retrieval
from app.config import settings
from app.gcp.vertex_index import VertexIndex
from app.retrieval import Index, aretrieve, make_chunks, retrieve
from tests.test_retrieval import make_book


class FakeEmbedder:
    def embed_query(self, text):
        return np.ones(4, dtype=np.float32)


def row(i, book_id="b", end=None):
    return {"data": {"book_id": book_id, "chunk_id": f"r{i}", "section": 0, "para_start": i,
                     "para_end": i + 1 if end is None else end, "speaker": "narrator", "text": f"chunk {i}"},
            "distance": 0.1}


class FakeStore:
    """Ignores filters on purpose, like a misbehaving backend; records what it was asked."""
    def __init__(self, vec, text=()):
        self.vec, self.text, self.calls = list(vec), list(text), []

    async def vector_search(self, vector, filter, top_k):
        self.calls.append(("vector", filter, top_k))
        return self.vec

    async def text_search(self, query, filter, top_k):
        self.calls.append(("text", filter, top_k))
        return self.text


def run(c):
    return asyncio.run(c)


def test_post_filter_drops_future_chunks_even_if_store_returns_them():
    store = FakeStore([row(50), row(3), row(8)], [row(40), row(5)])
    idx = VertexIndex(make_book(), FakeEmbedder(), store)
    out = run(idx.asearch("q", reader_para=10, k=8))
    assert out and all(c.para_end <= 10 for c in out)
    assert {c.id for c in out} == {"r3", "r8", "r5"}
    assert [c.para_start for c in out] == sorted(c.para_start for c in out)


def test_filter_and_top_k_passed_to_store():
    store = FakeStore([row(1)])
    run(VertexIndex(make_book(), FakeEmbedder(), store).asearch("q", 10, 4))
    flt = {"$and": [{"book_id": {"$eq": "b"}}, {"para_end": {"$lte": 10}}]}
    assert sorted(store.calls, key=lambda c: c[0]) == [("text", flt, 16), ("vector", flt, 16)]
    assert store.calls[0][0] == "text", "text search starts before the embedding is ready"


def test_textbook_path_has_no_para_filter_and_no_post_filter():
    store = FakeStore([row(50)])
    book = make_book(kind="textbook")
    out = run(retrieval.aretrieve(book, VertexIndex(book, FakeEmbedder(), store), "q", 10))
    assert store.calls[0][1] == {"book_id": {"$eq": "b"}}
    assert any(p["para_end"] == 51 for p in out["passages"])
    assert out["mode"] == "vertex" and out["story_so_far"] == ""


def test_other_books_and_malformed_rows_excluded():
    bad = {"data": {"book_id": "b", "chunk_id": "x", "para_start": 1, "text": "t"}}
    store = FakeStore([row(1, book_id="other"), bad, row(2)])
    out = run(VertexIndex(make_book(), FakeEmbedder(), store).asearch("q", 10, 4))
    assert [c.id for c in out] == ["r2"]


def test_limited_to_k():
    store = FakeStore([row(i) for i in range(8)])
    assert len(run(VertexIndex(make_book(), FakeEmbedder(), store).asearch("q", 20, 3))) == 3


def test_aretrieve_matches_retrieve_for_local_index():
    book = make_book()
    idx = Index(make_chunks(book))
    assert run(aretrieve(book, idx, "secretword5", 12)) == retrieve(book, idx, "secretword5", 12)


def test_aretrieve_vertex_never_leaks_future_passages_into_output():
    book = make_book()
    store = FakeStore([row(30), row(6)])
    out = run(aretrieve(book, VertexIndex(book, FakeEmbedder(), store), "q", 10))
    assert all(p["para_end"] <= 10 for p in out["passages"])


def test_index_for_returns_vertex_index_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    d = tmp_path / "books" / "b"
    d.mkdir(parents=True)
    (d / "book.json").write_text(make_book().model_dump_json(), encoding="utf-8")
    main._index_for.cache_clear()
    idx = main._index_for("b")
    assert isinstance(idx, VertexIndex) and idx.mode == "vertex"
    main._index_for.cache_clear()
