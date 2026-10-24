"""Several uploads at once (#18): each book is its own job; a small global limit bounds CPU work and paid indexing,
and one book never gets two indexing jobs."""
import asyncio
import threading
import time

from app import ingest_upload, main
from app.config import settings
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)


def _books(n):
    return [ingest_upload.build_book(f"u-b{i}-0000000{i}", "T", "A", "textbook", "One paragraph.") for i in range(n)]


def test_indexing_runs_a_few_jobs_at_once_and_queues_the_rest(client, monkeypatch):
    state = {"now": 0, "max": 0}
    seen_queued = []

    async def fake_index(book, chunks, embedder, store, on_progress=None):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        seen_queued.extend(b for b, st in main._upload_status.items() if (st.get("index") or {}).get("state") == "queued")
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return 0

    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    books = _books(4)

    async def run():
        await asyncio.gather(*(main._index_in_background(b, []) for b in books))

    asyncio.run(run())
    assert state["max"] == main.INDEX_CONCURRENCY == 2
    assert seen_queued   # the waiting books said "queued"
    assert all(main._upload_status[b.id]["index"]["state"] == "done" for b in books)
    assert main._index_jobs == set()


def test_one_indexing_job_per_book(client, monkeypatch):
    calls = []

    async def fake_index(book, chunks, embedder, store, on_progress=None):
        calls.append(book.id)
        await asyncio.sleep(0.02)
        return 0

    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    book = _books(1)[0]

    async def run():
        await asyncio.gather(main._index_in_background(book, []), main._index_in_background(book, []))

    asyncio.run(run())
    assert calls == [book.id]   # never two paid embedding jobs for one book


def test_queued_indexing_counts_as_busy_and_is_interrupted_by_a_restart(client):
    bid = post(client, "a.txt", b"One para.\n\nTwo para.", kind="textbook", title="Q").json()["book_id"]
    main._set_index(bid, state="queued", done=0, total=10)
    assert main._book_busy(bid)
    assert client.delete(f"/api/books/{bid}").status_code == 409
    main._upload_status.pop(bid)   # a restart: only status.json is left
    st = client.get(f"/api/books/{bid}/status").json()
    assert st["index"]["state"] == "error"


def test_several_uploads_are_accepted_at_once(client):
    """No more "another book is still being processed" for the second file of a multi-file drop."""
    g = main.IngestGuard()
    tokens = [g.acquire() for _ in range(main.UPLOAD_QUEUE_MAX)]
    assert all(t is not None for t in tokens) and len(set(tokens)) == len(tokens)
    assert g.acquire() is None   # the queue is bounded (each pending upload holds its bytes in memory)
    g.release(tokens[0])
    assert g.acquire() is not None


def test_concurrent_ingests_are_limited_and_the_waiting_one_says_queued(client, monkeypatch):
    running = {"now": 0, "max": 0}
    lock = threading.Lock()
    real = ingest_upload.process_upload
    queued_msgs = []

    def slow(*a, **k):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            queued_msgs.extend(st.get("message") for st in main._upload_status.values() if st.get("status") == "processing")
        time.sleep(0.05)
        with lock:
            running["now"] -= 1
        return real(*a, **k)

    monkeypatch.setattr(ingest_upload, "process_upload", slow)
    ids = [f"u-p{i}-0000000{i}" for i in range(4)]

    async def run():
        jobs = []
        for bid in ids:
            token = main._ingest_guard.acquire()
            main._set_status(bid, "processing", "Queued")
            jobs.append(main._ingest(bid, "T", "A", "textbook", b"One para.\n\nTwo para.", ".txt", token))
        await asyncio.gather(*jobs)

    asyncio.run(run())
    assert running["max"] == main.INGEST_CONCURRENCY == 2
    assert "Queued" in queued_msgs
    assert all(main._upload_status[b]["status"] == "ready" for b in ids)
    assert not main._ingest_guard.busy
    assert settings.retrieval_backend == "local"
