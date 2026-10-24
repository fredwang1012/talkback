"""Uploaded novels get what the built-in books get (#115): attribution of untagged lines, series and volume order."""
import json

from app import books
from app.config import settings
from app.enrich import cast as cast_mod
from app.enrich import pipeline
from app.schema import Book, Paragraph, Section, Summary
from tests.test_novel_pipeline import FakeProvider, data_dir, make_book_on_disk, run_pipeline, saved  # noqa: F401

EXCHANGE = "\n\n".join([
    "Chapter 1",
    "m0x The rain fell on the road.",
    'm1x "Where are you going?" asked Alice.',
    '"Home, of course," said Bob.',
    '"Why so early?"',
    "m4x Nobody spoke after that.",
]) + "\n"


class SeriesFake(FakeProvider):
    def __init__(self, series=None, index=None, **kw):
        super().__init__(**kw)
        self.series, self.index = series, index

    async def complete(self, system, user, schema, tier, max_tokens, effort):
        raw = await super().complete(system, user, schema, tier, max_tokens, effort)
        if schema["title"] == "MainCast":
            body = json.loads(raw.text) | {"series": self.series, "series_index": self.index}
            raw = raw.model_copy(update={"text": json.dumps(body)})
        return raw


# ---------- attribution (#17) runs on every upload ----------

def test_untagged_reply_is_attributed_even_without_minor_voices(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "minor_voices", False)
    make_book_on_disk(data_dir, EXCHANGE)
    ok, events, _ = run_pipeline("u-test-aaaaaaaa")
    assert ok
    book = saved(data_dir)
    reply = next(p for p in book.paragraphs if p.text == '"Why so early?"')
    assert [s.speaker for s in reply.segments] == ["alice"]  # alternation: Alice, Bob, then Alice again
    assert {c.speaker for c in reply.tts_chunks} == {"alice"}
    assert not any(c.main is False for c in book.characters)  # no stand-ins without MINOR_VOICES
    assert any(e.get("message") == pipeline.ATTRIBUTE_MESSAGE for e in events)


def test_untagged_reply_is_attributed_with_minor_voices_too(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "minor_voices", True)
    make_book_on_disk(data_dir, EXCHANGE)
    ok, _, _ = run_pipeline("u-test-aaaaaaaa")
    reply = next(p for p in saved(data_dir).paragraphs if p.text == '"Why so early?"')
    assert ok and [s.speaker for s in reply.segments] == ["alice"]


# ---------- series and volume order ----------

def test_series_slug():
    assert cast_mod.series_slug("Harry Potter") == "harry-potter"
    assert cast_mod.series_slug("The Saga Chronicles series") == "saga"
    assert cast_mod.series_slug("  ") is None and cast_mod.series_slug(None) is None
    assert cast_mod.series_slug("A Song of Ice & Fire") == "a-song-of-ice-fire"


def test_cast_call_sets_series_and_volume(data_dir):
    make_book_on_disk(data_dir)
    ok, _, _ = run_pipeline("u-test-aaaaaaaa", SeriesFake("The Saga Chronicles", 2))
    book = saved(data_dir)
    assert ok and (book.series, book.series_index) == ("saga", 2)
    assert "series" in cast_mod.UPLOAD_SYSTEM


def test_standalone_book_and_bad_index_get_no_series_order(data_dir):
    make_book_on_disk(data_dir)
    run_pipeline("u-test-aaaaaaaa", SeriesFake(None, None))
    assert (saved(data_dir).series, saved(data_dir).series_index) == (None, None)
    make_book_on_disk(data_dir, book_id="u-test-bbbbbbbb")
    run_pipeline("u-test-bbbbbbbb", SeriesFake("Saga", 0))
    b = saved(data_dir, "u-test-bbbbbbbb")
    assert (b.series, b.series_index) == ("saga", None)


def test_series_already_set_is_kept(data_dir):
    book = make_book_on_disk(data_dir)
    book.series, book.series_index = "mine", 5
    books.save_book(data_dir / "books" / book.id / "book.json", book)
    run_pipeline("u-test-aaaaaaaa", SeriesFake("Other", 1))
    assert (saved(data_dir).series, saved(data_dir).series_index) == ("mine", 5)


def test_estimate_counts_the_series_fields(data_dir):
    book = make_book_on_disk(data_dir)
    assert pipeline.estimate(book, "gemini") > 0
    assert pipeline.CAST_OUT_TOKENS >= 3000 + cast_mod.SERIES_OUT_TOKENS


# ---------- earlier books come from the same library only ----------

def _write(tmp, bid: str, idx: int, owner: str | None = None, end: str = "end"):
    b = Book(id=bid, title=bid, author="A", kind="novel", license="x",
             sections=[Section(id="s1", title="One", start_para=0)],
             paragraphs=[Paragraph(idx=j, section_id="s1", text=f"p{j}.") for j in range(3)],
             summaries=[Summary(upto_para=2, text=f"{end} of {bid}")], series="saga", series_index=idx)
    d = tmp / "books" / bid
    d.mkdir(parents=True, exist_ok=True)
    (d / "book.json").write_text(b.model_dump_json(), encoding="utf-8")
    if owner:
        (d / "owner.txt").write_text(owner, encoding="utf-8")


def test_earlier_in_series_never_mixes_owners_or_builtins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    books._series_index = None
    _write(tmp_path, "builtin-1", 1)
    _write(tmp_path, "builtin-2", 2)
    _write(tmp_path, "u-mine-1", 1, owner="alice")
    _write(tmp_path, "u-theirs-1", 1, owner="bob")
    _write(tmp_path, "u-mine-2", 2, owner="alice")
    assert [b.id for b in books.earlier_in_series(books.load_book("u-mine-2"))] == ["u-mine-1"]
    assert [b.id for b in books.earlier_in_series(books.load_book("builtin-2"))] == ["builtin-1"]
    assert "u-theirs-1" not in books.series_recap(books.load_book("u-mine-2"))
