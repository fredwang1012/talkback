"""DELETE /api/books/{id}: uploaded books only, owner only, never mid-processing, gone from disk, caches and Vertex.

The card leaves the library at once (book.json goes in the request); the slow part (one Vertex DELETE per chunk, then
the folder) runs as a background task with its status on /status, and a retry resumes it."""
import asyncio

import pytest
from fastapi import BackgroundTasks

from app import main, users
from app.config import settings
from tests.test_per_user import ALICE, BOB, h, pu, token  # noqa: F401  (pu is a fixture)
from tests.test_api import env  # noqa: F401
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)


class FakeStore:
    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    async def delete(self, ids):
        if self.fail:
            raise RuntimeError("boom")
        self.deleted.append(list(ids))


def upload(client, title="Gone Soon"):
    r = post(client, "a.txt", b"First paragraph here.\n\nSecond paragraph here.\n\nThird.", kind="textbook", title=title)
    assert r.status_code == 202
    return r.json()["book_id"]


def seed_extras(root, bid):
    for rel in (f"cache/avatar/{bid}/c1.mp4", f"portraits/{bid}/cover.png"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_delete_removes_everything(client, tmp_path):
    bid = upload(client)
    seed_extras(tmp_path, bid)
    client.get(f"/api/books/{bid}")   # warm the body cache
    assert bid in {b["id"] for b in client.get("/api/books").json()}
    r = client.delete(f"/api/books/{bid}")
    assert r.status_code == 202 and r.json() == {"deleted": bid, "status": "deleting"}
    assert bid not in {b["id"] for b in client.get("/api/books").json()}
    assert client.get(f"/api/books/{bid}").status_code == 404
    assert client.get(f"/api/books/{bid}/status").status_code == 404
    assert bid not in main._book_bodies and bid not in main._upload_status
    leftovers = [p for p in tmp_path.rglob("*") if bid in p.as_posix()]
    assert leftovers == []   # no folder, upload, avatar clips, portraits left
    assert client.delete(f"/api/books/{bid}").status_code == 404


def test_shipped_and_bad_ids_are_protected(client, tmp_path):
    (tmp_path / "books" / "demo").mkdir(parents=True)
    (tmp_path / "books" / "demo" / "book.json").write_text("{}")
    r = client.delete("/api/books/demo")
    assert r.status_code == 403 and (tmp_path / "books" / "demo" / "book.json").exists()
    assert client.delete("/api/books/u-nope-1").status_code == 404
    assert client.delete("/api/books/..%2Fx").status_code in (404, 405)


@pytest.mark.parametrize("mark", ["processing", "enrich", "job", "claim"])
def test_refused_while_busy(client, tmp_path, mark):
    bid = upload(client)
    if mark == "processing":
        main._upload_status[bid] = {"status": "processing"}
    elif mark == "enrich":
        main._upload_status[bid] = {"status": "ready", "enrich": {"status": "running"}}
    elif mark == "job":
        main._enrich_jobs.add(bid)
    else:
        main._kind_claims.add(bid)
    try:
        assert client.delete(f"/api/books/{bid}").status_code == 409
        assert (tmp_path / "books" / bid / "book.json").exists()
    finally:
        main._enrich_jobs.discard(bid)
        main._kind_claims.discard(bid)
        main._upload_status.pop(bid, None)


def test_vertex_datapoints_deleted_by_id(client, tmp_path, monkeypatch):
    bid = upload(client)
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    store = FakeStore()
    monkeypatch.setattr(main, "_vertex_deps", lambda: (object(), store))
    from app.gcp.indexing import datapoint_ids
    expected = datapoint_ids(main.books.load_book(bid))
    assert expected and all(i.startswith(f"{bid}__") for i in expected)
    assert client.delete(f"/api/books/{bid}").status_code == 202
    assert store.deleted == [expected]
    assert not (tmp_path / "books" / bid).exists()


def test_vertex_failure_is_reported_and_a_retry_resumes(client, tmp_path, monkeypatch):
    bid = upload(client)
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    from app.gcp.indexing import datapoint_ids
    expected = datapoint_ids(main.books.load_book(bid))
    monkeypatch.setattr(main, "_vertex_deps", lambda: (object(), FakeStore(fail=True)))
    assert client.delete(f"/api/books/{bid}").status_code == 202
    assert bid not in {b["id"] for b in client.get("/api/books").json()}   # gone from the library anyway
    st = client.get(f"/api/books/{bid}/status").json()
    assert st["status"] == "delete_error" and "search" in st["message"]
    assert (tmp_path / "books" / bid / "deleting.json").exists()   # the datapoint ids are kept for the retry
    assert main._kind_claims == set()   # the claim is released so a retry works
    store = FakeStore()
    monkeypatch.setattr(main, "_vertex_deps", lambda: (object(), store))
    assert client.delete(f"/api/books/{bid}").status_code == 202
    assert store.deleted == [expected]   # the same ids, though book.json is gone
    assert not (tmp_path / "books" / bid).exists()
    assert client.get(f"/api/books/{bid}/status").status_code == 404


def test_partial_disk_failure_still_hides_the_book(client, tmp_path, monkeypatch):
    bid = upload(client)
    import shutil
    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)   # a FUSE tree removal that does nothing
    r = client.delete(f"/api/books/{bid}")
    assert r.status_code == 202
    assert bid not in {b["id"] for b in client.get("/api/books").json()}   # book.json went first
    assert (tmp_path / "books" / bid).is_dir()
    monkeypatch.setattr(shutil, "rmtree", real)
    assert client.delete(f"/api/books/{bid}").status_code == 202   # a retry finishes the job
    assert not (tmp_path / "books" / bid).exists()


def test_only_the_owner_deletes(pu, tmp_path):
    client, _ = pu
    assert client.delete("/api/books/u-alice-1", headers=h(token(BOB))).status_code == 404
    assert client.delete("/api/books/u-alice-1").status_code == 404
    assert (settings.data_dir / "books" / "u-alice-1" / "book.json").exists()
    cast = users.cast_json("u-alice-1", users.Identity(ALICE, users.ns_for(ALICE)))
    cast.parent.mkdir(parents=True, exist_ok=True)
    cast.write_text("{}")
    assert client.delete("/api/books/u-alice-1", headers=h(token(ALICE))).status_code == 202
    assert not (settings.data_dir / "books" / "u-alice-1").exists() and not cast.exists()
    assert (settings.data_dir / "books" / "u-bob-1" / "book.json").exists()   # Bob's book untouched
    assert client.delete("/api/books/demo", headers=h(token(ALICE))).status_code == 403
    assert client.delete("/api/books/hp1", headers=h(token(ALICE))).status_code == 404


def test_anonymous_cannot_delete_even_shared_namespace_uploads(pu):
    client, _ = pu
    r = client.post("/api/books/upload", data={"title": "Anon", "kind": "textbook"},
                    files={"file": ("a.txt", b"Hello there.\n\nSecond paragraph here.", "text/plain")})
    bid = r.json()["book_id"]
    assert (settings.data_dir / "books" / bid / "owner.txt").read_text() == users.PUBLIC_NS
    assert client.delete(f"/api/books/{bid}").status_code == 403
    assert (settings.data_dir / "books" / bid).exists()


def test_owner_marker_must_match_even_for_owner_only_ids(pu):
    client, _ = pu
    (settings.data_dir / "books" / "u-alice-1" / "owner.txt").write_text(users.ns_for(BOB))
    assert client.delete("/api/books/u-alice-1", headers=h(token(ALICE))).status_code == 404
    assert (settings.data_dir / "books" / "u-alice-1" / "book.json").exists()


def test_an_upload_still_being_ingested_is_never_resume_deleted(client, tmp_path):
    """No book.json yet (ingest in flight) must not look like a half-finished delete."""
    bid = "u-inflight-0000abcd"
    d = tmp_path / "books" / bid
    d.mkdir(parents=True)
    (d / "status.json").write_text('{"status": "processing"}')
    main._upload_status[bid] = {"status": "processing"}
    try:
        assert client.delete(f"/api/books/{bid}").status_code == 409
        main._upload_status.pop(bid)   # even with no in-memory status (no tombstone, no book.json): refused
        assert client.delete(f"/api/books/{bid}").status_code == 409
        assert d.is_dir() and (d / "status.json").exists()
    finally:
        main._upload_status.pop(bid, None)


def test_a_failed_local_delete_leaves_a_tombstone_and_resumes(client, tmp_path, monkeypatch):
    bid = upload(client)
    import shutil
    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    assert client.delete(f"/api/books/{bid}").status_code == 202
    assert (tmp_path / "books" / bid / "deleting.json").exists()   # local backend too
    main._delete_status.clear()   # as after a restart: only the tombstone on disk says "resume"
    monkeypatch.setattr(shutil, "rmtree", real)
    assert client.delete(f"/api/books/{bid}").status_code == 202
    assert not (tmp_path / "books" / bid).exists()


def test_refused_while_vertex_indexing(client, tmp_path):
    bid = upload(client)
    main._upload_status[bid] = {"status": "ready", "index": {"state": "running", "done": 400, "total": 3000}}
    try:
        assert client.delete(f"/api/books/{bid}").status_code == 409
        assert (tmp_path / "books" / bid / "book.json").exists()
    finally:
        main._upload_status.pop(bid, None)


def test_the_card_goes_before_the_slow_part_and_a_retry_joins_it(client, tmp_path, monkeypatch):
    """The response comes before the Vertex deletes; a second DELETE meanwhile neither 409s nor deletes twice."""
    bid = upload(client)
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    calls = []

    class SlowStore:
        async def delete(self, ids):
            calls.append(len(ids))
            await asyncio.sleep(0.05)

    monkeypatch.setattr(main, "_vertex_deps", lambda: (object(), SlowStore()))

    async def run():
        first = BackgroundTasks()
        r = await main.delete_book(bid, first)
        assert r.status_code == 202 and calls == []   # answered before any Vertex call
        assert bid not in {b["id"] for b in main.books.list_books()}
        assert main.book_status(bid)["status"] == "deleting"
        job = asyncio.ensure_future(first())
        await asyncio.sleep(0.01)   # the job is now inside the slow Vertex delete
        second = BackgroundTasks()
        r2 = await main.delete_book(bid, second)
        assert r2.status_code == 202
        await second()
        await job

    asyncio.run(run())
    assert calls != [] and len(calls) == 1 and not (tmp_path / "books" / bid).exists()
    assert main._deleting == {} and main._kind_claims == set()


def test_a_reupload_during_a_delete_is_a_new_book(client, tmp_path, monkeypatch):
    bid = upload(client, title="Same Title")

    async def run():
        bg = BackgroundTasks()
        await main.delete_book(bid, bg)
        return bg

    bg = asyncio.run(run())
    new = upload(client, title="Same Title")   # while the old one is still being removed
    assert new != bid
    asyncio.run(bg())
    assert not (tmp_path / "books" / bid).exists()
    assert (tmp_path / "books" / new / "book.json").exists()
    assert new in {b["id"] for b in client.get("/api/books").json()}


def test_start_enrichment_refused_during_a_delete(client):
    bid = upload(client)
    main._upload_status[bid] = {"status": "ready", "enrich": {"status": "awaiting_confirmation"}}
    main._kind_claims.add(bid)
    try:
        assert client.post(f"/api/books/{bid}/enrich").status_code == 409
    finally:
        main._kind_claims.discard(bid)
        main._upload_status.pop(bid, None)
