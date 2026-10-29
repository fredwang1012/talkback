"""Local meaning-based search for uploads (RETRIEVAL_BACKEND=local): after an upload is published, its chunks get e5
passage embeddings in the background (one book at a time), saved in the shipped books' index format, so retrieval
scores it hybrid. Never the real model here: a fake embedder stands in."""
import asyncio
import threading

import numpy as np
import pytest

from app import indexes, ingest_upload, local_index, processing, retrieval
from app.config import settings
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)

TEXT = "\n\n".join(f"Paragraph {i} about the dragon and the castle by the sea." * 3 for i in range(40)).encode()


class FakeEmbedder:
    """Deterministic unit vectors; records how many passages it saw and on which thread."""
    def __init__(self, dim=8):
        self.dim, self.calls, self.threads = dim, 0, set()

    def _vec(self, text):
        rng = np.random.default_rng(abs(hash(text)) % (2 ** 32))
        v = rng.normal(size=self.dim)
        return v / np.linalg.norm(v)

    def embed_passages(self, texts, batch_size=32):
        self.calls += len(texts)
        self.threads.add(threading.get_ident())
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def on(monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(settings, "local_embed_uploads", True)
    monkeypatch.setattr(processing, "_local_embed_available", lambda: True)
    monkeypatch.setattr(indexes, "_embedder", lambda: fake)
    return fake


def _book(bid="u-emb-00000001"):
    return ingest_upload.build_book(bid, "T", "A", "textbook", TEXT.decode())


def _publish(book):
    chunks = retrieval.make_chunks(book)
    retrieval.save_index(settings.data_dir / "books" / book.id / "index", chunks, None)
    ingest_upload.publish_book(book)
    return chunks


def test_embed_chunks_batches_in_a_thread_and_reports_progress():
    fake = FakeEmbedder()
    chunks = [retrieval.Chunk(id=f"r{i}", para_start=i, para_end=i, text=f"t{i}") for i in range(70)]
    seen = []
    emb = asyncio.run(local_index.embed_chunks(chunks, fake, batch=32, on_progress=lambda d, t: seen.append((d, t))))
    assert emb.shape == (70, 8) and emb.dtype == np.float32
    assert seen == [(32, 70), (64, 70), (70, 70)]
    assert threading.get_ident() not in fake.threads   # never on the event loop's thread


def test_save_embeddings_refuses_when_the_chunks_changed(tmp_path):
    chunks = [retrieval.Chunk(id="r0", para_start=0, para_end=0, text="a")]
    retrieval.save_index(tmp_path, chunks, None)
    other = [retrieval.Chunk(id="r0", para_start=0, para_end=0, text="b")]
    assert not local_index.save_embeddings(tmp_path, other, np.ones((1, 4)))
    assert not (tmp_path / "embeddings.npy").exists()
    assert local_index.save_embeddings(tmp_path, chunks, np.ones((1, 4)))
    assert np.load(tmp_path / "embeddings.npy").shape == (1, 4)
    assert not local_index.save_embeddings(tmp_path / "gone", chunks, np.ones((1, 4)))   # deleted folder: no write
    assert not (tmp_path / "gone").exists()


def test_save_index_without_embeddings_drops_stale_ones(tmp_path):
    chunks = [retrieval.Chunk(id="r0", para_start=0, para_end=0, text="a")]
    retrieval.save_index(tmp_path, chunks, np.ones((1, 4)))
    retrieval.save_index(tmp_path, chunks, None)   # re-chunked text: old vectors no longer match
    assert not (tmp_path / "embeddings.npy").exists()


def test_index_load_ignores_embeddings_of_the_wrong_length(tmp_path):
    chunks = [retrieval.Chunk(id=f"r{i}", para_start=i, para_end=i, text="a") for i in range(3)]
    retrieval.save_index(tmp_path, chunks, np.ones((2, 4)))
    idx = retrieval.Index.load(tmp_path, embedder=FakeEmbedder())
    assert idx.mode == "bm25"


def test_upload_is_embedded_and_served_hybrid(client, on, monkeypatch):
    started = []
    monkeypatch.setattr(processing, "_start_local_embed", lambda book, chunks: started.append((book, chunks)))
    bid = post(client, "a.txt", TEXT, kind="textbook", title="Emb").json()["book_id"]
    assert [b.id for b, _ in started] == [bid]   # started once the book was published
    assert indexes._index_for(bid).mode == "bm25"   # BM25 serves meanwhile
    asyncio.run(processing._embed_locally(*started[0]))
    st = client.get(f"/api/books/{bid}/status").json()
    assert st["index"]["state"] == "done"
    idx_dir = settings.data_dir / "books" / bid / "index"
    n = len(retrieval.Index.load(idx_dir).chunks)
    assert np.load(idx_dir / "embeddings.npy").shape == (n, 8) and on.calls == n
    assert indexes._index_for(bid).mode == "hybrid"   # the cache was cleared, the new vectors are used


def test_job_is_one_book_at_a_time(client, on, monkeypatch):
    state = {"now": 0, "max": 0}
    real = local_index.embed_chunks

    async def tracked(chunks, embedder, **kw):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.02)
        try:
            return await real(chunks, embedder, **kw)
        finally:
            state["now"] -= 1

    monkeypatch.setattr(local_index, "embed_chunks", tracked)
    bks = [_book(f"u-emb-0000000{i}") for i in range(3)]
    pairs = [(b, _publish(b)) for b in bks]

    async def run():
        await asyncio.gather(*(processing._embed_locally(b, c) for b, c in pairs))

    asyncio.run(run())
    assert state["max"] == 1
    assert all((settings.data_dir / "books" / b.id / "index" / "embeddings.npy").is_file() for b in bks)
    assert processing._local_embed_jobs == set()


def test_skips_gracefully_without_sentence_transformers(client, monkeypatch):
    monkeypatch.setattr(settings, "local_embed_uploads", True)
    monkeypatch.setattr(processing, "_local_embed_available", lambda: False)
    b = _book()
    asyncio.run(processing._embed_locally(b, _publish(b)))
    assert "index" not in (processing._upload_status.get(b.id) or {})   # no "indexing" line at all
    assert not (settings.data_dir / "books" / b.id / "index" / "embeddings.npy").exists()
    assert indexes._index_for(b.id).mode == "bm25"


def test_model_load_failure_keeps_bm25(client, monkeypatch):
    monkeypatch.setattr(settings, "local_embed_uploads", True)
    monkeypatch.setattr(processing, "_local_embed_available", lambda: True)
    monkeypatch.setattr(indexes, "_embedder", lambda: None)   # model download failed
    b = _book()
    asyncio.run(processing._embed_locally(b, _publish(b)))
    st = processing._upload_status[b.id]["index"]
    assert st["state"] == "error" and st["message"] == processing.INDEX_FAILED_MESSAGE
    assert indexes._index_for(b.id).mode == "bm25"


def test_delete_stops_the_job(client, on, monkeypatch):
    b = _book()
    chunks = _publish(b)
    real = on.embed_passages

    def deleting(texts, batch_size=32):   # the reader deletes the book while the first batch is being embedded
        processing._deleted.add(b.id)
        return real(texts, batch_size)

    monkeypatch.setattr(on, "embed_passages", deleting)
    try:
        asyncio.run(processing._embed_locally(b, chunks))
        assert on.calls <= 32   # stopped after the batch in flight
        assert not (settings.data_dir / "books" / b.id / "index" / "embeddings.npy").exists()
    finally:
        processing._deleted.discard(b.id)


def test_local_embedding_does_not_block_delete(client, on):
    b = _book()
    _publish(b)
    processing._local_embed_jobs.add(b.id)
    processing._set_index(b.id, state="running", done=1, total=10)
    try:
        assert not processing._book_busy(b.id)
    finally:
        processing._local_embed_jobs.discard(b.id)


def test_rechunked_meanwhile_reruns_on_the_new_chunks(client, on, monkeypatch):
    b = _book()
    old = _publish(b)
    idx_dir = settings.data_dir / "books" / b.id / "index"
    new = old[:-1]   # the text was re-read (kind change) while the first job ran

    real = local_index.embed_chunks
    started = []

    async def slow(chunks, embedder, **kw):
        started.append(len(chunks))
        if len(started) == 1:
            retrieval.save_index(idx_dir, new, None)
            await processing._embed_locally(b, new)   # second request while the first runs: queued as a rerun
        return await real(chunks, embedder, **kw)

    monkeypatch.setattr(local_index, "embed_chunks", slow)
    asyncio.run(processing._embed_locally(b, old))
    assert started == [len(old), len(new)]
    assert np.load(idx_dir / "embeddings.npy").shape[0] == len(new)


def test_vertex_mode_does_not_embed_locally(client, on, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    b = _book()
    asyncio.run(processing._embed_locally(b, _publish(b)))
    assert on.calls == 0


# ---------- startup backfill: built-in books and older uploads without embeddings ----------

def _builtin(bid="alice-test"):
    b = ingest_upload.build_book(bid, "Alice", "Carroll", "novel", TEXT.decode())
    _publish(b)
    return b


def test_startup_embeds_older_uploads_first_then_builtin_books(client, on, monkeypatch):
    builtin, upload = _builtin(), _book("u-old-00000001")
    _publish(upload)
    order = []
    real = local_index.embed_chunks

    async def tracked(chunks, embedder, **kw):
        order.append(len(order))
        return await real(chunks, embedder, **kw)

    seen = []
    monkeypatch.setattr(local_index, "embed_chunks", tracked)
    real_job = processing._embed_book_locally

    async def job(book_id, chunks):
        seen.append(book_id)
        await real_job(book_id, chunks)

    monkeypatch.setattr(processing, "_embed_book_locally", job)
    asyncio.run(processing._backfill_local_embeddings())
    assert seen == [upload.id, builtin.id]   # uploads waiting go first
    for b in (builtin, upload):
        assert indexes._index_for(b.id).mode == "hybrid"
    # a built-in book gets no status.json (it would dirty a clone's tree); the upload card shows its indexing line
    assert not (settings.data_dir / "books" / builtin.id / "status.json").exists()
    assert processing._upload_status[upload.id]["index"]["state"] == "done"


def test_startup_skips_books_that_already_have_embeddings(client, on):
    b = _builtin()
    asyncio.run(processing._backfill_local_embeddings())
    calls = on.calls
    asyncio.run(processing._backfill_local_embeddings())
    assert on.calls == calls   # nothing left to embed


def test_startup_backfill_off_in_vertex_mode_or_without_the_package(client, on, monkeypatch):
    _builtin()
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    asyncio.run(processing._backfill_local_embeddings())
    monkeypatch.setattr(settings, "retrieval_backend", "local")
    monkeypatch.setattr(processing, "_local_embed_available", lambda: False)
    asyncio.run(processing._backfill_local_embeddings())
    assert on.calls == 0


def test_embeddings_files_are_gitignored():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("embeddings.npy", "embeddings.npy.tmp"):
        r = subprocess.run(["git", "check-ignore", "-q", f"data/books/alice-in-wonderland/index/{name}"], cwd=root)
        assert r.returncode == 0, name
