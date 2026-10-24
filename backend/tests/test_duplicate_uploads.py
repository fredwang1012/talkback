"""Duplicate uploads (#82): the same file (sha256 of its bytes) is added once per account. A second upload gets a 409
with the existing book id; while that book is being deleted the upload is refused until the delete finishes; two
simultaneous uploads of one file make one book."""
import asyncio
import time

import httpx
from fastapi import BackgroundTasks

from app import ingest_upload, main, users
from app.config import settings
from tests.test_api import env  # noqa: F401
from tests.test_per_user import ALICE, BOB, h, pu, token  # noqa: F401  (pu is a fixture)
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)

TXT = b"First paragraph here.\n\nSecond paragraph here.\n\nThird."


def test_the_same_file_twice_points_at_the_first_copy(client):
    first = post(client, "a.txt", TXT, kind="textbook", title="Original")
    assert first.status_code == 202
    bid = first.json()["book_id"]
    again = post(client, "renamed.txt", TXT, kind="textbook", title="Another Title")
    assert again.status_code == 409
    body = again.json()
    assert body["reason"] == "duplicate" and body["book_id"] == bid and body["title"] == "Original"
    assert "Original" in body["detail"]
    assert [b["id"] for b in client.get("/api/books").json() if b["id"].startswith("u-")] == [bid]


def test_a_different_file_is_a_new_book(client):
    a = post(client, "a.txt", TXT, kind="textbook").json()["book_id"]
    r = post(client, "b.txt", TXT + b"\n\nOne more paragraph.", kind="textbook")
    assert r.status_code == 202 and r.json()["book_id"] != a


def test_the_hash_is_per_account(pu):
    client, _ = pu
    send = lambda who: client.post("/api/books/upload", headers=h(token(who)), data={"title": "Shared", "kind": "textbook"},  # noqa: E731
                                   files={"file": ("a.txt", TXT, "text/plain")})
    alice = send(ALICE)
    assert alice.status_code == 202
    assert send(BOB).status_code == 202   # Bob never learns that Alice has this file
    again = send(ALICE)
    assert again.status_code == 409 and again.json()["book_id"] == alice.json()["book_id"]


def test_refused_while_the_same_book_is_being_deleted(client, tmp_path):
    bid = post(client, "a.txt", TXT, kind="textbook").json()["book_id"]

    async def start_delete():
        bg = BackgroundTasks()
        await main.delete_book(bid, bg)
        return bg

    bg = asyncio.run(start_delete())   # the card is gone; the slow part has not run yet
    r = post(client, "a.txt", TXT, kind="textbook")
    assert r.status_code == 409
    assert r.json()["reason"] == "deleting" and "still being deleted" in r.json()["detail"]
    asyncio.run(bg())   # the delete finishes
    r = post(client, "a.txt", TXT, kind="textbook")
    assert r.status_code == 202 and r.json()["book_id"] != bid


def test_a_leftover_tombstone_with_no_delete_running_does_not_block(client, tmp_path):
    """After a restart a half-finished delete is not running: the file can be added again as a new book."""
    bid = post(client, "a.txt", TXT, kind="textbook").json()["book_id"]
    d = tmp_path / "books" / bid
    (d / "deleting.json").write_text("[]")
    (d / "book.json").unlink()
    r = post(client, "a.txt", TXT, kind="textbook")
    assert r.status_code == 202 and r.json()["book_id"] != bid


def test_a_failed_upload_can_be_retried(client):
    bad = b"%PDF-1.4 this is not really a pdf"
    first = post(client, "x.pdf", bad)
    assert first.status_code == 202
    assert client.get(f"/api/books/{first.json()['book_id']}/status").json()["status"] == "error"
    assert post(client, "x.pdf", bad).status_code == 202   # no book was made, so nothing to point at


def test_an_upload_still_in_flight_is_a_duplicate(client):
    digest = ingest_upload.content_hash(TXT)
    main._hash_claims[("", digest)] = "u-inflight-0000abcd"
    try:
        r = post(client, "a.txt", TXT, kind="textbook")
        assert r.status_code == 409 and r.json()["reason"] == "processing"
        assert r.json()["book_id"] == "u-inflight-0000abcd"
    finally:
        main._hash_claims.clear()


def test_two_simultaneous_uploads_of_one_file_make_one_book(client, monkeypatch):
    real = ingest_upload.find_upload

    def slow(*a, **k):   # both requests are inside the lookup at the same time
        time.sleep(0.1)
        return real(*a, **k)

    monkeypatch.setattr(ingest_upload, "find_upload", slow)

    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            send = lambda: ac.post("/api/books/upload", data={"title": "Twin", "kind": "textbook"},  # noqa: E731
                                   files={"file": ("a.txt", TXT, "text/plain")})
            return await asyncio.gather(send(), send())

    codes = sorted(r.status_code for r in asyncio.run(run()))
    assert codes == [202, 409]
    assert main._hash_claims == {}   # released once the ingest ended
    assert len([b for b in main.books.list_books() if b["id"].startswith("u-")]) == 1


def test_the_hash_is_stored_with_the_upload(client, tmp_path):
    bid = post(client, "a.txt", TXT, kind="textbook").json()["book_id"]
    digest = ingest_upload.content_hash(TXT)
    assert ingest_upload.find_upload(digest, users.LOCAL) == (bid, "ready", "My Test Book!")
    assert settings.data_dir == tmp_path
