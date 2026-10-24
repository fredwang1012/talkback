"""Chunk-rules version: books built with older TTS chunk rules are re-chunked once, on load, without losing styles."""
import sys
import threading
from pathlib import Path

import pytest

from app import books
from app.config import settings
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment
from app.tts_cache import cache_key
from app.ttschunks import CHUNK_RULES_VERSION, chunk_paragraph, rechunk_book

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "preprocess"))


def novel() -> Book:
    """p0 opens a chapter (current rules add a long_pause); p1 is a line the enrich LLM marked as shouting
    (no '!' and no 'shouted' cue, so the rules alone would not find it)."""
    paras = [
        Paragraph(idx=0, section_id="s0", text="It was dark.", segments=[Segment(speaker="narrator", text="It was dark.")]),
        Paragraph(idx=1, section_id="s0", text='"Hello there," said Alice.',
                  segments=[Segment(speaker="alice", text='"Hello there,"', style="shouting"),
                            Segment(speaker="narrator", text="said Alice.")]),
    ]
    b = Book(id="old-novel", title="T", author="A", kind="novel", license="x",
             sections=[Section(id="s0", title="Chapter 1", start_para=0)], paragraphs=paras,
             characters=[Character(id="alice", name="Alice", first_para=1, voice="v_alice", persona="p")],
             narrator=Narrator(voice="marcus"))
    rechunk_book(b)
    return b


def old_novel() -> Book:
    """As saved before the version existed: no chunk_rules, and p0's chunk built by older rules."""
    b = novel()
    b.chunk_rules = 0
    b.paragraphs[0].tts_chunks[0].tts_text = "It was dark."
    return b


def write(tmp_path, monkeypatch, book: Book) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "auto_rechunk", True, raising=False)
    d = tmp_path / "books" / book.id
    d.mkdir(parents=True)
    path = d / "book.json"
    raw = book.model_dump_json(indent=1)
    if not book.chunk_rules:  # a file from before the field existed
        import json
        data = json.loads(raw)
        data.pop("chunk_rules", None)
        raw = json.dumps(data, indent=1)
    path.write_text(raw, encoding="utf-8")
    return path


@pytest.fixture
def saves(monkeypatch):
    calls = []
    real = books.save_book

    def counting(path, book):
        calls.append(path)
        real(path, book)
    monkeypatch.setattr(books, "save_book", counting)
    return calls


def test_book_json_without_the_field_still_loads():
    raw = old_novel().model_dump_json()
    assert '"chunk_rules"' in raw
    import json
    data = json.loads(raw)
    del data["chunk_rules"]
    assert Book.model_validate(data).chunk_rules == 0


def test_rechunk_book_stamps_the_version():
    b = novel()
    assert b.chunk_rules == CHUNK_RULES_VERSION >= 1


def test_old_book_is_upgraded_on_load_with_styles_kept(tmp_path, monkeypatch, saves):
    path = write(tmp_path, monkeypatch, old_novel())
    first = books.load_book("old-novel")
    assert first is not None  # served at once (old chunks) while the upgrade runs
    books.wait_for_upgrades()
    assert len(saves) == 1
    on_disk = Book.model_validate_json(path.read_text(encoding="utf-8"))
    assert on_disk.chunk_rules == CHUNK_RULES_VERSION
    assert on_disk.paragraphs[0].tts_chunks[0].tts_text == "<|prosody:long_pause|>It was dark."
    assert on_disk.paragraphs[1].segments[0].style == "shouting"  # the LLM's style survives
    assert "<|style:shouting|>" in on_disk.paragraphs[1].tts_chunks[0].tts_text
    assert not path.with_name("book.json.tmp").exists()
    again = books.load_book("old-novel")
    assert again.chunk_rules == CHUNK_RULES_VERSION and again is not first  # the cache picks up the new file
    books.wait_for_upgrades()
    assert len(saves) == 1


def test_upgrade_does_not_write_cached_portraits(tmp_path, monkeypatch, saves):
    path = write(tmp_path, monkeypatch, old_novel())
    (tmp_path / "portraits" / "old-novel").mkdir(parents=True)
    (tmp_path / "portraits" / "old-novel" / "alice.png").write_bytes(b"x")
    books.load_book("old-novel")
    books.wait_for_upgrades()
    assert Book.model_validate_json(path.read_text(encoding="utf-8")).characters[0].portrait is None


def test_current_book_is_not_rewritten(tmp_path, monkeypatch, saves):
    path = write(tmp_path, monkeypatch, novel())
    before = path.read_bytes()
    assert books.load_book("old-novel").chunk_rules == CHUNK_RULES_VERSION
    books.wait_for_upgrades()
    assert saves == [] and path.read_bytes() == before


def test_upgrade_is_off_when_disabled(tmp_path, monkeypatch, saves):
    write(tmp_path, monkeypatch, old_novel())
    monkeypatch.setattr(settings, "auto_rechunk", False)
    books.load_book("old-novel")
    books.wait_for_upgrades()
    assert saves == []


def test_concurrent_loads_write_once(tmp_path, monkeypatch, saves):
    write(tmp_path, monkeypatch, old_novel())
    gate = threading.Barrier(8)

    def load():
        gate.wait()
        books.load_book("old-novel")
    threads = [threading.Thread(target=load) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    books.wait_for_upgrades()
    books.load_book("old-novel")
    books.wait_for_upgrades()
    assert len(saves) == 1


def test_failed_save_keeps_the_old_file_and_the_book_is_still_served(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, old_novel())
    before = path.read_bytes()

    def boom(p, b):
        raise OSError("read-only file system")
    monkeypatch.setattr(books, "save_book", boom)
    assert books.load_book("old-novel") is not None
    books.wait_for_upgrades()
    assert path.read_bytes() == before
    book = books.load_book("old-novel")
    assert book is not None and book.paragraphs[0].tts_chunks[0].tts_text == "It was dark."
    books.wait_for_upgrades()  # no retry loop on the same file


def test_a_broken_paragraph_keeps_the_old_file(tmp_path, monkeypatch, saves):
    b = old_novel()
    b.paragraphs[1].segments[0].text = "words that are not in the paragraph"
    path = write(tmp_path, monkeypatch, b)
    before = path.read_bytes()
    assert books.load_book("old-novel") is not None
    books.wait_for_upgrades()
    assert saves == [] and path.read_bytes() == before


def test_file_changed_meanwhile_is_not_overwritten(tmp_path, monkeypatch, saves):
    """An upgrade that read an older file must not clobber a newer one (e.g. an enrich stage saved in between)."""
    path = write(tmp_path, monkeypatch, old_novel())
    real_rechunk = books.rechunk_book

    def slow(book):
        real_rechunk(book)
        newer = old_novel()
        newer.title = "Changed by someone else"
        path.write_text(newer.model_dump_json(indent=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(books, "rechunk_book", slow)
    books.load_book("old-novel")
    books.wait_for_upgrades()
    assert saves == []
    assert "Changed by someone else" in path.read_text(encoding="utf-8")


def test_unchanged_chunks_keep_their_tts_cache_key(tmp_path, monkeypatch, saves):
    old = old_novel()
    path = write(tmp_path, monkeypatch, old)
    books.load_book("old-novel")
    books.wait_for_upgrades()
    new = Book.model_validate_json(path.read_text(encoding="utf-8"))
    key = lambda c: cache_key(c.tts_text, c.voice)  # noqa: E731
    assert [key(c) for c in new.paragraphs[1].tts_chunks] == [key(c) for c in old.paragraphs[1].tts_chunks]
    assert key(new.paragraphs[0].tts_chunks[0]) != key(old.paragraphs[0].tts_chunks[0])  # re-voiced only when played


def test_textbook_upgrade_matches_a_fresh_ingest(tmp_path, monkeypatch, saves):
    from app.ingest_upload import CODE_MARK, build_book
    raw = f"Chapter 1 Intro\n\nSome text here. More text.\n\n{CODE_MARK}def f():\n    return 1\n\nAfter the code."
    fresh = build_book("u-tb", "T", "A", "textbook", raw)
    assert fresh.chunk_rules == CHUNK_RULES_VERSION  # ingest stamps the version
    texts = [c.tts_text for p in fresh.paragraphs for c in p.tts_chunks]
    assert any("long_pause" in t for t in texts) and "Code listing skipped." in texts
    stale = fresh.model_copy(deep=True)
    stale.chunk_rules = 0
    for p in stale.paragraphs:
        for c in p.tts_chunks:
            c.tts_text = c.tts_text.replace("<|prosody:long_pause|>", "")
    path = write(tmp_path, monkeypatch, stale)
    books.load_book("u-tb")
    books.wait_for_upgrades()
    upgraded = Book.model_validate_json(path.read_text(encoding="utf-8"))
    assert [p.tts_chunks for p in upgraded.paragraphs] == [p.tts_chunks for p in fresh.paragraphs]


def test_schedule_all_upgrades_covers_every_book(tmp_path, monkeypatch, saves):
    write(tmp_path, monkeypatch, old_novel())
    b2 = old_novel()
    b2.id = "other-novel"
    write(tmp_path, monkeypatch, b2)
    books.schedule_all_upgrades()
    books.wait_for_upgrades()
    assert len(saves) == 2


def test_chunk_tts_keeps_styles_by_default_and_stamps(tmp_path):
    import chunk_tts
    path = tmp_path / "book.json"
    path.write_text(old_novel().model_dump_json(indent=1), encoding="utf-8")
    chunk_tts.rechunk_file(path)
    b = Book.model_validate_json(path.read_text(encoding="utf-8"))
    assert b.chunk_rules == CHUNK_RULES_VERSION
    assert b.paragraphs[1].segments[0].style == "shouting"
    assert "<|style:shouting|>" in b.paragraphs[1].tts_chunks[0].tts_text


def test_chunk_tts_redetect_styles_flag_clears_llm_styles(tmp_path):
    import chunk_tts
    path = tmp_path / "book.json"
    path.write_text(old_novel().model_dump_json(indent=1), encoding="utf-8")
    chunk_tts.rechunk_file(path, redetect_styles=True)
    b = Book.model_validate_json(path.read_text(encoding="utf-8"))
    assert b.paragraphs[1].segments[0].style is None
    assert "<|style:" not in b.paragraphs[1].tts_chunks[0].tts_text


def test_chunk_paragraph_does_not_clear_styles():
    b = novel()
    chunk_paragraph(b.paragraphs[1], b)
    assert b.paragraphs[1].segments[0].style == "shouting"
