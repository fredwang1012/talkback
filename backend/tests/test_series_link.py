"""#134: a series is found even when the first volume does not say it is one (library reconciliation)."""
import asyncio

import pytest

from app import books
from app.config import settings
from app.enrich import cast as cast_mod
from app.enrich import pipeline, series_link, spend
from app.enrich.llm import SpendLimit
from app.schema import Book, Paragraph, Section


def mk(bid, title, author="Frank Herbert", series=None, idx=None, kind="novel") -> Book:
    return Book(id=bid, title=title, author=author, kind=kind, license="x",
                sections=[Section(id="s1", title="One", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text="Text.")], series=series, series_index=idx)


def put(tmp_path, book):
    d = tmp_path / "books" / book.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "book.json").write_text(book.model_dump_json(), encoding="utf-8")


class FakeLLM:
    """Answers by title from `table` {title: (member, index)}; records every question."""
    def __init__(self, table):
        self.table, self.asked, self.usd, self.calls = table, [], 0.0, 0

    async def complete_json(self, system, user, schema_model, tier="fast", **kw):
        self.asked.append(user)
        self.calls += 1
        for title, (member, idx) in self.table.items():
            if f"'{title}'" in user:
                return schema_model(member=member, index=idx)
        return schema_model(member=False, index=None)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


# ---- prompt and slug ----

def test_prompt_counts_first_volume_and_short_name():
    p = cast_mod.UPLOAD_SYSTEM
    assert "first book" in p and "Dune" in p and "Philosopher" in p and "short" in p


@pytest.mark.parametrize("name", ["Dune", "Dune Chronicles", "The Dune saga", "Dune series", "The Dune Chronicles series",
                                  "Dune Cycle", "Dune sequence"])
def test_series_slug_dune_forms(name):
    assert cast_mod.series_slug(name) == "dune"


def test_series_slug_keeps_harry_potter():
    assert cast_mod.series_slug("The Harry Potter series") == "harry-potter"
    assert cast_mod.series_slug("Harry Potter") == "harry-potter"
    assert cast_mod.series_slug(None) is None


# ---- reconciliation ----

def test_forward_other_upload_without_series_joins(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    llm = FakeLLM({"Dune": (True, 1)})
    assert run(series_link.reconcile("u-mess", llm)) == 1
    b = books.load_book("u-dune")
    assert (b.series, b.series_index) == ("dune", 1)
    assert "'Dune' by Frank Herbert" in llm.asked[0] and "'dune'" in llm.asked[0]


def test_reverse_book_without_series_finds_one(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    run(series_link.reconcile("u-dune", FakeLLM({"Dune": (True, 1)})))
    b = books.load_book("u-dune")
    assert (b.series, b.series_index) == ("dune", 1)


def test_non_member_not_applied_and_not_asked_twice(lib):
    put(lib, mk("u-other", "Heretics"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    llm = FakeLLM({})
    run(series_link.reconcile("u-mess", llm))
    run(series_link.reconcile("u-mess", llm))
    assert llm.calls == 1
    assert books.load_book("u-other").series is None


def test_never_other_author_builtin_textbook_or_other_owner(lib, monkeypatch):
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    put(lib, mk("u-x", "Other Book", author="Someone Else"))
    put(lib, mk("dune-builtin", "Dune"))
    put(lib, mk("u-text", "Dune Notes", kind="textbook"))
    put(lib, mk("u-foreign", "Dune Foreign"))
    monkeypatch.setattr(books, "same_library", lambda a, b: b != "u-foreign")
    llm = FakeLLM({})
    assert run(series_link.reconcile("u-mess", llm)) == 0
    assert llm.calls == 0


def test_author_match_is_normalized(lib):
    put(lib, mk("u-dune", "Dune", author="Herbert, Frank"))
    put(lib, mk("u-mess", "Dune Messiah", author="FRANK  HERBERT.", series="dune", idx=2))
    assert run(series_link.reconcile("u-mess", FakeLLM({"Dune": (True, 1)}))) == 1


def test_cap_of_five_checks(lib):
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    for i in range(8):
        put(lib, mk(f"u-b{i}", f"Book {i}"))
    llm = FakeLLM({})
    assert run(series_link.reconcile("u-mess", llm)) == 5
    assert llm.calls == 5


def test_bad_index_is_dropped_but_membership_kept(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    run(series_link.reconcile("u-mess", FakeLLM({"Dune": (True, 500)})))
    b = books.load_book("u-dune")
    assert b.series == "dune" and b.series_index is None


def test_save_happens_under_the_write_lock(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    path = lib / "books" / "u-dune" / "book.json"
    held = []
    real = books.update_book

    def spy(p, change):
        held.append(books.write_lock(p))
        return real(p, change)
    monkeypatch.setattr(books, "update_book", spy)
    run(series_link.reconcile("u-mess", FakeLLM({"Dune": (True, 1)})))
    assert held and held[0] is books.write_lock(path)


def test_llm_failure_is_swallowed_and_retried_later(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))

    class Boom:
        async def complete_json(self, *a, **k):
            raise RuntimeError("down")
    assert run(series_link.reconcile("u-mess", Boom())) == 0
    assert run(series_link.reconcile("u-mess", FakeLLM({"Dune": (True, 1)}))) == 1  # not marked as checked


def test_spend_limit_stops_quietly(lib):
    put(lib, mk("u-a", "A"))
    put(lib, mk("u-b", "B"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))

    class Capped:
        calls = 0

        async def complete_json(self, *a, **k):
            Capped.calls += 1
            raise SpendLimit("budget")
    run(series_link.reconcile("u-mess", Capped()))
    assert Capped.calls == 1


def test_skipped_when_total_cap_reached(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    monkeypatch.setattr(spend, "total_usd", lambda: settings.upload_total_cap_usd)
    assert run(series_link.reconcile("u-mess", FakeLLM({"Dune": (True, 1)}))) == 0


# ---- startup pass ----

def test_startup_fixes_existing_uploads_once(lib):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    llm = FakeLLM({"Dune": (True, 1)})
    assert run(series_link.startup_pass(llm)) == 1
    assert books.load_book("u-dune").series == "dune"
    assert run(series_link.startup_pass(llm)) == 0
    assert llm.calls == 1


def test_startup_marks_non_members_so_they_are_not_asked_again(lib):
    put(lib, mk("u-x", "Heretics"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    llm = FakeLLM({})
    run(series_link.startup_pass(llm))
    run(series_link.startup_pass(llm))
    assert llm.calls == 1


def test_startup_total_is_bounded(lib):
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    for i in range(30):
        put(lib, mk(f"u-b{i}", f"Book {i}"))
    llm = FakeLLM({})
    run(series_link.startup_pass(llm))
    assert llm.calls <= series_link.STARTUP_MAX_CHECKS


# ---- pipeline ----

def test_estimate_includes_the_series_checks(lib):
    b = mk("u-dune", "Dune")
    floor = series_link.check_estimate_usd("gemini") * series_link.MAX_CHECKS
    assert floor > 0
    assert pipeline.estimate(b, "gemini") >= floor


def test_enrich_runs_reconciliation_at_the_end(lib, monkeypatch):
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    seen = []

    async def fake_reconcile(book_id, llm, **kw):
        seen.append(book_id)
        return 0

    async def fake_stages(book, report, llm, **kw):
        return False
    monkeypatch.setattr(series_link, "reconcile", fake_reconcile)
    monkeypatch.setattr(pipeline, "run_stages", fake_stages)
    ok = run(pipeline.enrich_book("u-mess", lambda **f: None, llm=FakeLLM({})))
    assert ok and seen == ["u-mess"]


# ---- startup pass: LLM down, books being enriched, ordering (#141 review) ----

def test_startup_stops_after_a_failed_call(lib):
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    for i in range(4):
        put(lib, mk(f"u-b{i}", f"Book {i}"))

    class Down:
        calls = 0

        async def complete_json(self, *a, **k):
            Down.calls += 1
            raise TimeoutError("llm down")
    assert run(series_link.startup_pass(Down())) == 1   # the failed call counts as attempted
    assert Down.calls == 1   # LLM down: one timeout per startup, not one per candidate


def test_startup_skips_books_being_enriched(lib):
    from app import ingest_upload
    put(lib, mk("u-dune", "Dune"))
    put(lib, mk("u-mess", "Dune Messiah", series="dune", idx=2))
    for state in ("running", "queued"):
        ingest_upload.write_status("u-dune", "ready", enrich={"status": state, "stage": "cast"})
        llm = FakeLLM({"Dune": (True, 1)})
        assert run(series_link.startup_pass(llm)) == 0 and llm.calls == 0   # its own enrichment checks at the end
    assert books.load_book("u-dune").series is None
    ingest_upload.write_status("u-dune", "ready", enrich={"status": "error", "message": "x"})
    assert run(series_link.startup_pass(FakeLLM({"Dune": (True, 1)}))) == 1


def test_warm_up_loads_indexes_before_the_series_pass(lib, monkeypatch):
    from app import indexes, main
    order = []
    monkeypatch.setattr(settings, "auto_rechunk", False)
    monkeypatch.setattr(settings, "retrieval_backend", "local", raising=False)
    monkeypatch.setattr(books, "list_books", lambda: [{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(indexes, "_index_for", lambda bid: order.append(("index", bid)))

    async def series():
        order.append(("series",))
        return 0
    monkeypatch.setattr(series_link, "startup_default", series)
    main._warm_up()
    assert order == [("index", "a"), ("index", "b"), ("series",)]
