"""Every book.json writer shares one per-book lock, and save_book never shares a temp file (review of #133)."""
import json
import threading
import time

import pytest

from app import books, ingest_upload
from app.enrich import textbook as tb
from app.schema import Summary

from tests.test_chunk_rules import old_novel, write


def test_save_book_uses_a_unique_temp_file_in_the_same_dir(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, old_novel())
    seen = []
    real = books.os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        real(src, dst)
    monkeypatch.setattr(books.os, "replace", spy)
    books.save_book(path, old_novel())
    books.save_book(path, old_novel())
    assert len(seen) == 2 and seen[0][0] != seen[1][0]
    for src, dst in seen:
        assert dst == str(path) and src.startswith(str(path.parent)) and src.endswith(".tmp")


def test_concurrent_saves_publish_a_valid_book(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, old_novel())
    errors = []

    def worker(n):
        b = old_novel()
        b.title = f"T{n}"
        try:
            for _ in range(5):
                books.save_book(path, b)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8"))["title"].startswith("T")
    assert sorted(p.name for p in path.parent.iterdir()) == ["book.json"]


WRITERS = {
    "save_book": lambda path: books.save_book(path, old_novel()),
    "update_equations": lambda path: ingest_upload.update_equations("old-novel", {"none": {"latex": "x"}}),
    "update_book": lambda path: books.update_book(path, lambda b: setattr(b, "kind", "textbook")),
    "save_summaries": lambda path: tb.save_summaries("old-novel", [Summary(upto_para=0, text="x")]),
}


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_writers_wait_for_the_book_lock(tmp_path, monkeypatch, name):
    path = write(tmp_path, monkeypatch, old_novel())
    lock = books.write_lock(path)
    with lock:
        t = threading.Thread(target=WRITERS[name], args=(path,))
        t.start()
        t.join(0.3)
        assert t.is_alive(), f"{name} wrote while another writer held the lock"
    t.join(5)
    assert not t.is_alive()


def test_upgrade_rechecks_the_stamp_inside_the_lock(tmp_path, monkeypatch):
    """A writer that holds the lock while the rechunk runs and then saves wins; the upgrade must not overwrite it."""
    path = write(tmp_path, monkeypatch, old_novel())
    rechunked = threading.Event()
    real = books.rechunk_book

    def signalling(book):
        real(book)
        rechunked.set()
    monkeypatch.setattr(books, "rechunk_book", signalling)
    with books.write_lock(path):
        books.load_book("old-novel")
        assert rechunked.wait(5)
        time.sleep(0.2)   # the upgrade now waits for the lock
        newer = old_novel()
        newer.title = "Saved by the other writer"
        books.save_book(path, newer)
    books.wait_for_upgrades()
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "Saved by the other writer"


def test_publish_book_goes_through_save_book(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, old_novel())
    calls = []
    monkeypatch.setattr(books, "save_book", lambda p, b: calls.append(p))
    ingest_upload.publish_book(old_novel())
    assert calls == [path]
