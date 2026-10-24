"""One unreadable book.json must not take the whole library down (it is skipped and logged)."""
from app import books
from app.config import ROOT, settings


def test_a_broken_book_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    good = tmp_path / "books" / "good"
    good.mkdir(parents=True)
    src = next(p for p in (ROOT / "data" / "books").glob("*/book.json") if p.parent.name == "alice-in-wonderland")
    good.joinpath("book.json").write_text(src.read_text(encoding="utf-8").replace('"alice-in-wonderland"', '"good"', 1), encoding="utf-8")
    bad = tmp_path / "books" / "bad"
    bad.mkdir()
    bad.joinpath("book.json").write_text('{"id": "bad", "paragraphs": [{"kind": "hologram"}]}', encoding="utf-8")
    ids = [b["id"] for b in books.list_books()]
    assert "good" in ids and "bad" not in ids
