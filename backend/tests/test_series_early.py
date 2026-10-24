"""Early series detection right after an upload is published (fake LLM only, no paid calls)."""
import asyncio

import pytest

from app import books, public_client
from app.config import settings
from app.enrich import series_early, series_link, spend
from app.enrich.llm import SpendLimit
from app.schema import Book, Paragraph, Section


def mk(bid, title, author="Frank Herbert", series=None, idx=None, kind="novel", text="In the week before."):
    return Book(id=bid, title=title, author=author, kind=kind, license="x",
                sections=[Section(id="s1", title="One", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text=text)], series=series, series_index=idx)


def put(tmp_path, book):
    d = tmp_path / "books" / book.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "book.json").write_text(book.model_dump_json(), encoding="utf-8")


class FakeLLM:
    def __init__(self, early=("Dune", 1), member=(False, None), raises=None):
        self.early, self.member, self.raises, self.early_users, self.link_users = early, member, raises, [], []

    async def complete_json(self, system, user, schema_model, tier="fast", **kw):
        if self.raises:
            raise self.raises
        if schema_model is series_early.EarlyGuess:
            self.early_users.append(user)
            return schema_model(series=self.early[0], series_index=self.early[1])
        self.link_users.append(user)
        return schema_model(member=self.member[0], index=self.member[1])


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(spend, "_state", None)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_sets_series_and_index(lib):
    put(lib, mk("u-dune", "Dune"))
    llm = FakeLLM(("The Dune Chronicles series", 1))
    assert run(series_early.detect("u-dune", llm)) is True
    b = books.load_book("u-dune")
    assert (b.series, b.series_index) == ("dune", 1)
    assert "Title: Dune" in llm.early_users[0] and "In the week before." in llm.early_users[0]


def test_excerpt_is_bounded(lib):
    put(lib, mk("u-dune", "Dune", text="x" * 9000))
    llm = FakeLLM()
    run(series_early.detect("u-dune", llm))
    assert len(llm.early_users[0]) < 1800


def test_null_answer_and_bad_index(lib):
    put(lib, mk("u-a", "Standalone"))
    assert run(series_early.detect("u-a", FakeLLM((None, None)))) is False
    assert books.load_book("u-a").series is None
    put(lib, mk("u-b", "Dune"))
    run(series_early.detect("u-b", FakeLLM(("Dune", 500))))
    b = books.load_book("u-b")
    assert b.series == "dune" and b.series_index is None


def test_keeps_existing_series_without_a_call(lib):
    put(lib, mk("u-dune", "Dune", series="arrakis", idx=3))
    llm = FakeLLM(("Dune", 1))
    assert run(series_early.detect("u-dune", llm)) is False
    assert llm.early_users == []
    assert books.load_book("u-dune").series == "arrakis"


def test_series_set_meanwhile_is_not_overwritten(lib):
    put(lib, mk("u-dune", "Dune"))

    class Racing(FakeLLM):
        async def complete_json(self, *a, **kw):
            out = await super().complete_json(*a, **kw)
            books.update_book(books._books_dir() / "u-dune" / "book.json", lambda b: setattr(b, "series", "hand"))
            return out
    assert run(series_early.detect("u-dune", Racing())) is False
    assert books.load_book("u-dune").series == "hand"


def test_only_novel_uploads(lib):
    put(lib, mk("u-text", "Algebra", kind="textbook"))
    put(lib, mk("demo", "Dune"))
    llm = FakeLLM()
    assert run(series_early.detect("u-text", llm)) is False
    assert run(series_early.detect("demo", llm)) is False
    assert run(series_early.detect("u-missing", llm)) is False
    assert llm.early_users == []


def test_over_the_cap_makes_no_call(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    monkeypatch.setattr(settings, "upload_total_cap_usd", 0.0)
    llm = FakeLLM()
    assert run(series_early.detect("u-dune", llm)) is False and llm.early_users == []


def test_failures_never_raise_or_set(lib):
    put(lib, mk("u-dune", "Dune"))
    assert run(series_early.detect("u-dune", FakeLLM(raises=RuntimeError("boom")))) is False
    assert run(series_early.detect("u-dune", FakeLLM(raises=SpendLimit("cap")))) is False
    assert books.load_book("u-dune").series is None


def test_reconciles_same_author_uploads_afterwards(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    put(lib, mk("u-new", "Children of Dune"))
    llm = FakeLLM(("Dune", 1), member=(True, 3))
    run(series_early.detect("u-dune", llm))   # Dune gets series; Children of Dune is then asked about
    assert books.load_book("u-new").series == "dune"
    assert llm.link_users


def test_default_without_llm_does_nothing(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(settings, "public_api_url", "")
    monkeypatch.setattr(public_client, "local_llm_configured", lambda: False)
    assert run(series_early.detect_default("u-dune")) is False
    monkeypatch.setattr(settings, "upload_enrich", False)
    assert run(series_early.detect_default("u-dune")) is False


def test_default_on_a_clone_uses_the_public_route(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(public_client, "active", lambda: True)
    posts = []

    async def fake_post(path, body, timeout, client=None):
        posts.append((path, body))
        return {"series": "Dune", "series_index": 1}
    monkeypatch.setattr(public_client, "_post", fake_post)
    assert run(series_early.detect_default("u-dune")) is True
    assert posts[0][0] == "/api/public/series-early" and "Title: Dune" in posts[0][1]["text"]
    assert books.load_book("u-dune").series == "dune"


def test_write_goes_through_the_write_lock(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    seen = []
    real = books.update_book
    monkeypatch.setattr(books, "update_book", lambda path, change: (seen.append(str(path)), real(path, change))[1])
    run(series_early.detect("u-dune", FakeLLM()))
    assert seen and seen[0].endswith("book.json")
