"""Textbook recap notes: one study-guide summary per chapter (and per numbered subsection), with a fake LLM.

No network, no credit. Covers the section mapping, the estimate and the caps (refuse before, stop during), the upload
and confirm flows, the public service path, and how the recap treats textbooks (no spoilers) versus novels (unchanged).
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import books, main, public_api, public_client, recap
from app.config import settings
from app.enrich import pipeline, spend
from app.enrich import textbook as tb
from app.enrich.llm import LLM, Raw
from app.ratelimit import TokenBucket
from app.schema import Book, Paragraph, Section, Summary

MARK = re.compile(r"m\d+x")
BOOK_ID = "u-tb-aaaaaaaa"


class FakeLLM:
    """Answers SectionSummary calls with the paragraph markers it was shown (so tests see what each call read)."""
    name = "fake"

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[str, str]] = []  # (system, user)
        self.fail_on = fail_on

    async def complete(self, system, user, schema, tier, max_tokens, effort) -> Raw:
        assert schema["title"] == "SectionSummary", schema["title"]
        self.calls.append((system, user))
        if self.fail_on and self.fail_on in user:
            raise RuntimeError("boom")
        body = json.dumps({"summary": "Covers " + " ".join(sorted(set(MARK.findall(user)), key=lambda m: int(m[1:-1]))) + "."})
        return Raw(text=body, in_tokens=len(user) // 4 + 50, out_tokens=len(body) // 4 + 10, model="fake")


def make_llm(fake=None) -> LLM:
    return LLM("gemini", {"gemini": fake or FakeLLM()})


def textbook() -> Book:
    """Ch 1 (paras 0-7) with 1.1 (2-4) and 1.2 (5-7); Ch 2 (8-11) without subsections; 2.1.1-style level 3 inside 1.2."""
    secs = [Section(id="c1", title="1 Basics", start_para=0, level=1),
            Section(id="c1-1", title="1.1 Terms", start_para=2, level=2),
            Section(id="c1-2", title="1.2 Formulas", start_para=5, level=2),
            Section(id="c1-2-1", title="1.2.1 The idf", start_para=6, level=3),
            Section(id="c2", title="2 Ranking", start_para=8, level=1)]
    sid = lambda i: "c1" if i < 2 else "c1-1" if i < 5 else "c1-2" if i < 6 else "c1-2-1" if i < 8 else "c2"  # noqa: E731
    paras = [Paragraph(idx=i, section_id=sid(i), text=f"m{i}x Text of paragraph {i} about indexing.") for i in range(12)]
    return Book(id=BOOK_ID, title="IR", author="A", kind="textbook", license="n/a", sections=secs, paragraphs=paras)


def novel() -> Book:
    return Book(id="u-nv-aaaaaaaa", title="Story", author="A", kind="novel", license="n/a",
                sections=[Section(id="s1", title="Chapter One", start_para=0), Section(id="s2", title="Chapter Two", start_para=5)],
                paragraphs=[Paragraph(idx=i, section_id="s1" if i < 5 else "s2", text=f"Paragraph {i}.") for i in range(10)],
                summaries=[Summary(upto_para=4, text="Early notes."), Summary(upto_para=9, text="Late notes.")])


def markers(text: str) -> set[int]:
    return {int(m[1:-1]) for m in MARK.findall(text)}


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(spend, "_state", None)
    return tmp_path


def on_disk(data_dir: Path, book: Book | None = None) -> Book:
    book = book or textbook()
    (data_dir / "books" / book.id).mkdir(parents=True, exist_ok=True)
    books.save_book(data_dir / "books" / book.id / "book.json", book)
    return book


def saved(data_dir: Path, book_id: str = BOOK_ID) -> Book:
    return Book.model_validate_json((data_dir / "books" / book_id / "book.json").read_text(encoding="utf-8"))


# ---------- chapter / subsection mapping ----------

def test_targets_are_chapters_and_level_two_subsections_with_their_ranges():
    t = {x.section_id: (x.level, x.start, x.end, x.children) for x in tb.targets(textbook())}
    assert t == {"c1": (1, 0, 7, ("c1-1", "c1-2")), "c1-1": (2, 2, 4, ()), "c1-2": (2, 5, 7, ()), "c2": (1, 8, 11, ())}
    only = {x.section_id: (x.start, x.end, x.children) for x in tb.targets(textbook(), include_subsections=False)}
    assert only == {"c1": (0, 7, ()), "c2": (8, 11, ())}


def test_each_summary_reads_only_its_own_section_and_maps_to_it():
    fake = FakeLLM()
    out = asyncio.run(tb.build(textbook(), make_llm(fake)))
    by = {s.section_id: s for s in out}
    assert set(by) == {"c1", "c1-1", "c1-2", "c2"}
    assert {k: s.upto_para for k, s in by.items()} == {"c1": 7, "c1-1": 4, "c1-2": 7, "c2": 11}
    assert markers(by["c1-1"].text) == {2, 3, 4} and markers(by["c1-2"].text) == {5, 6, 7}
    assert markers(by["c2"].text) == {8, 9, 10, 11}
    # the chapter with subsections is written from its opening text plus its subsections' summaries (no re-read)
    assert markers(by["c1"].text) == set(range(8))
    chapter_call = next(u for _, u in fake.calls if "1.1 Terms" in u and "1 Basics" in u and "Subsection" in u)
    assert "m3x Text" not in chapter_call  # subsection text itself is not sent again
    assert all("Text of paragraph" in u or "Subsection" in u for _, u in fake.calls)
    assert len(fake.calls) == 4


def test_chapters_only_reads_each_chapter_whole():
    fake = FakeLLM()
    out = asyncio.run(tb.build(textbook(), make_llm(fake), include_subsections=False))
    assert {s.section_id: markers(s.text) for s in out} == {"c1": set(range(8)), "c2": {8, 9, 10, 11}}
    assert len(fake.calls) == 2


def test_failed_subsection_is_skipped_and_the_chapter_still_gets_a_summary():
    out = asyncio.run(tb.build(textbook(), make_llm(FakeLLM(fail_on="1.1 Terms\n"))))
    ids = {s.section_id for s in out}
    assert "c1-1" not in ids and {"c1", "c1-2", "c2"} <= ids


def test_math_paragraphs_are_read_as_their_spoken_form():
    from app.schema import MathBlock
    book = textbook()
    book.paragraphs[9].kind = "math"
    book.paragraphs[9].math = MathBlock(id="e1", page=0, bbox=[0, 0, 1, 1], spoken="idf equals log N over df")
    fake = FakeLLM()
    asyncio.run(tb.build(book, make_llm(fake), include_subsections=False))
    user = next(u for _, u in fake.calls if "2 Ranking" in u)
    assert "idf equals log N over df" in user and "m9x" not in user


def test_prompt_asks_for_a_short_study_guide_without_invented_content():
    system = tb.SECTION_SYSTEM.lower()
    assert "3 to 6 sentences" in system and "only" in system and "formula" in system and "definition" in system


# ---------- estimate and the subsection decision ----------

def test_estimate_grows_with_text_and_subsections_cost_little_more():
    small = textbook()
    big = textbook()
    for p in big.paragraphs:
        p.text = p.text + " filler" * 2000
    assert 0 < tb.estimate(small) < tb.estimate(big)
    with_subs, chapters = tb.estimate(big), tb.estimate(big, include_subsections=False)
    assert chapters < with_subs < chapters * 2


def test_plan_drops_subsections_when_only_chapters_fit_the_cap(monkeypatch):
    book = textbook()
    with_subs, chapters = tb.estimate(book), tb.estimate(book, include_subsections=False)
    assert tb.plan(book, cap=with_subs + 1) == (with_subs, True)
    assert tb.plan(book, cap=(with_subs + chapters) / 2) == (chapters, False)


def test_ir_sized_book_estimate_is_well_under_the_caps():
    """~1.1M characters, 32 chapters and 118 subsections, like the IR textbook: about a quarter of a dollar."""
    secs, paras = [], []
    for c in range(32):
        secs.append(Section(id=f"c{c}", title=f"{c + 1} Chapter", start_para=len(paras), level=1))
        for s in range(4 if c < 30 else 0):
            secs.append(Section(id=f"c{c}-{s}", title=f"{c + 1}.{s + 1} Part", start_para=len(paras), level=2))
            paras += [Paragraph(idx=len(paras) + i, section_id=f"c{c}-{s}", text="x" * 400) for i in range(21)]
        paras += [Paragraph(idx=len(paras) + i, section_id=f"c{c}", text="x" * 400) for i in range(5)]
    book = Book(id="u-ir-aaaaaaaa", title="IR", author="A", kind="textbook", license="n/a", sections=secs, paragraphs=paras)
    usd, subs = tb.plan(book, "gemini", cap=settings.upload_book_cap_usd)
    assert subs and 0.1 < usd < 1.0


# ---------- caps: refuse before, stop during ----------

def test_check_caps_uses_textbook_wording(monkeypatch):
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1.0)
    monkeypatch.setattr(settings, "upload_total_cap_usd", 2.0)
    monkeypatch.setattr(spend, "total_usd", lambda: 0.0)
    assert pipeline.check_caps(0.5, textbook=True) is None
    assert pipeline.check_caps(1.5, textbook=True) == pipeline.TEXTBOOK_TOO_LONG
    monkeypatch.setattr(spend, "total_usd", lambda: 1.8)
    assert pipeline.check_caps(0.5, textbook=True) == pipeline.TEXTBOOK_BUDGET_USED
    assert pipeline.check_caps(0.5) == pipeline.BUDGET_USED  # novels keep their wording


def run_textbook(book_id=BOOK_ID, fake=None):
    events: list[dict] = []
    ok = asyncio.run(pipeline.enrich_textbook(book_id, lambda **f: events.append(f), llm=make_llm(fake)))
    return ok, events


def test_enrich_textbook_saves_summaries_and_reports_like_novels(data_dir):
    on_disk(data_dir)
    ok, events = run_textbook()
    assert ok and events[0]["status"] == "running" and events[0]["stage"] == "summaries"
    assert events[0]["message"] == pipeline.TEXTBOOK_MESSAGE
    assert events[-1] == {"status": "done", "stage": "done", "progress": 1.0, "message": pipeline.TEXTBOOK_DONE}
    assert {s.section_id for s in saved(data_dir).summaries} == {"c1", "c1-1", "c1-2", "c2"}
    assert spend.total_usd() > 0 and (data_dir / "usage" / "upload_spend.json").is_file()


def test_total_cap_stops_a_running_job_and_keeps_what_was_written(data_dir, monkeypatch):
    on_disk(data_dir)
    monkeypatch.setattr(settings, "upload_total_cap_usd", 1e-9)  # the first call's usage passes it
    ok, events = run_textbook()
    assert not ok and events[-1]["status"] == "error" and events[-1]["message"] == pipeline.TEXTBOOK_BUDGET_USED
    assert len(saved(data_dir).summaries) <= 1


def test_book_cap_stops_a_running_job(data_dir, monkeypatch):
    on_disk(data_dir)
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1e-9)
    ok, events = run_textbook()
    assert not ok and events[-1]["message"] == pipeline.TEXTBOOK_TOO_LONG


def test_save_keeps_concurrent_equation_updates(data_dir):
    on_disk(data_dir)
    book = saved(data_dir)
    book.paragraphs[0].text = "changed by the equations job"
    books.save_book(data_dir / "books" / BOOK_ID / "book.json", book)
    assert tb.save_summaries(BOOK_ID, [Summary(upto_para=7, text="Chapter one.", section_id="c1")])
    after = saved(data_dir)
    assert after.paragraphs[0].text == "changed by the equations job" and after.summaries[0].section_id == "c1"


def test_save_skips_a_book_moved_to_novels(data_dir):
    book = textbook()
    book.kind = "novel"
    on_disk(data_dir, book)
    assert not tb.save_summaries(BOOK_ID, [Summary(upto_para=7, text="x", section_id="c1")])
    assert saved(data_dir).summaries == []


# ---------- upload flow (main.py) ----------

TB_RAW = ("Chapter 1 Basics\n\nm0x Intro text about indexes.\n\n1.1 Terms\n\nm1x A term is a word.\n\n"
          "1.2 Formulas\n\nm2x The idf is log N over df.\n\nChapter 2 Advanced\n\nm3x Ranking with cosine similarity.\n")


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(settings, "upload_confirm", False)
    main._upload_status.clear()
    monkeypatch.setattr(main, "_ingest_guard", main.IngestGuard())
    monkeypatch.setattr(main, "upload_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "_enrich_lock", asyncio.Lock())
    main._index_for.cache_clear()
    fake = FakeLLM()
    monkeypatch.setattr(pipeline, "make_llm", lambda *a, **k: make_llm(fake))
    c = TestClient(main.app)
    c.fake = fake
    return c


def upload_textbook(client):
    return client.post("/api/books/upload", files={"file": ("t.txt", TB_RAW.encode())},
                       data={"title": "IR", "author": "Me", "kind": "textbook"}).json()["book_id"]


def test_textbook_upload_gets_recap_notes_automatically(client):
    book_id = upload_textbook(client)
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["enrich"]["status"] == "done" and st["enrich"]["message"] == pipeline.TEXTBOOK_DONE
    assert st["enrich"]["summaries_only"] is True
    book = client.get(f"/api/books/{book_id}").json()
    assert book["characters"] == [] and {s["section_id"] for s in book["summaries"]} == {"s1", "s2", "s3", "s4"}
    r = client.get(f"/api/books/{book_id}/recap", params={"para": 0, "scope": "chapter"}).json()
    assert r["chapter"] == "Chapter 1 Basics" and r["text"]


def test_textbook_confirm_mode_waits_then_runs(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_confirm", True)
    book_id = upload_textbook(client)
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["enrich"]["status"] == "awaiting_confirmation" and st["enrich"]["estimate_usd"] >= 0
    assert "recap" in st["enrich"]["message"].lower() and not client.fake.calls
    assert client.post(f"/api/books/{book_id}/enrich").status_code == 202
    assert client.get(f"/api/books/{book_id}/status").json()["enrich"]["status"] == "done"
    assert client.get(f"/api/books/{book_id}").json()["summaries"]


def test_textbook_over_the_cap_is_refused_and_never_calls(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1e-9)
    book_id = upload_textbook(client)
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["enrich"]["status"] == "refused" and st["enrich"]["message"] == pipeline.TEXTBOOK_TOO_LONG
    assert not client.fake.calls and client.get(f"/api/books/{book_id}").json()["summaries"] == []


def test_novel_moved_to_textbooks_gets_textbook_notes(client):
    book_id = client.post("/api/books/upload", files={"file": ("t.txt", TB_RAW.encode())},
                          data={"title": "IR", "author": "Me", "kind": "textbook"}).json()["book_id"]
    client.fake.calls.clear()
    # pretend it was never summarised and was a novel, then move it to Textbooks
    path = settings.data_dir / "books" / book_id / "book.json"
    b = Book.model_validate_json(path.read_text(encoding="utf-8"))
    b.kind, b.summaries = "novel", []
    books.save_book(path, b)
    main._upload_status.pop(book_id, None)
    r = client.post(f"/api/books/{book_id}/kind", json={"kind": "textbook"})
    assert r.status_code == 202
    assert client.get(f"/api/books/{book_id}/status").json()["enrich"]["status"] == "done"
    assert any(s["section_id"] for s in client.get(f"/api/books/{book_id}").json()["summaries"])


def test_run_enrichment_dispatches_textbooks_and_uses_the_public_api_when_active(data_dir, monkeypatch):
    on_disk(data_dir)
    called = []

    async def fake_public(book_id, report, **kw):
        called.append("public")

    async def fake_local(book_id, report, **kw):
        called.append("local")

    async def novel_path(book_id, report, **kw):  # pragma: no cover - must not run for a textbook
        called.append("novel")

    monkeypatch.setattr(public_client, "summarize_textbook", fake_public)
    monkeypatch.setattr(pipeline, "enrich_textbook", fake_local)
    monkeypatch.setattr(pipeline, "enrich_book", novel_path)
    monkeypatch.setattr(public_client, "enrich_book", novel_path)
    monkeypatch.setattr(main, "_enrich_lock", asyncio.Lock())
    monkeypatch.setattr(public_client, "active", lambda: False)
    asyncio.run(main._run_enrichment(BOOK_ID))
    monkeypatch.setattr(public_client, "active", lambda: True)
    asyncio.run(main._run_enrichment(BOOK_ID))
    assert called == ["local", "public"]


# ---------- recap: textbooks have no spoilers, novels are unchanged ----------

def summarised() -> Book:
    book = textbook()
    book.summaries = [Summary(upto_para=4, text="Terms note.", section_id="c1-1"),
                      Summary(upto_para=7, text="Formulas note.", section_id="c1-2"),
                      Summary(upto_para=7, text="Chapter one note. It defines terms.", section_id="c1"),
                      Summary(upto_para=11, text="Chapter two note. It ranks.", section_id="c2")]
    return book


def test_textbook_chapter_recap_is_the_current_chapter_whatever_the_reader_position():
    book = summarised()
    for para in range(8):
        r = recap.scoped(book, para, "chapter")
        assert r["chapter"] == "1 Basics" and r["text"] == "Chapter one note. It defines terms." and r["upto_para"] == 7
    assert recap.scoped(book, 8, "chapter")["text"] == "Chapter two note. It ranks."


def test_textbook_any_section_can_be_recapped_from_anywhere():
    book = summarised()
    r = recap.scoped(book, 0, "chapter", section="c2")
    assert r["text"] == "Chapter two note. It ranks." and r["chapter"] == "2 Ranking"
    r = recap.scoped(book, 0, "chapter", section="c1-2")
    assert r["text"] == "Formulas note." and r["chapter"] == "1.2 Formulas"
    # a level-3 section without its own note: its subsection's note
    assert recap.scoped(book, 0, "chapter", section="c1-2-1")["text"] == "Formulas note."
    assert recap.scoped(book, 0, "chapter", section="nope")["text"] == ""


def test_textbook_book_scope_lists_the_chapters_up_to_the_reader():
    book = summarised()
    r = recap.scoped(book, 3, "book")
    assert "Chapter one note" in r["text"] and "Chapter two" not in r["text"]
    r = recap.scoped(book, 9, "book")
    assert r["text"].index("1 Basics") < r["text"].index("2 Ranking") and "It ranks" in r["text"]
    assert r["upto_para"] == 11 and r["chapter"] == "2 Ranking"


def test_textbook_without_section_notes_falls_back_to_the_old_behaviour():
    book = textbook()
    book.summaries = [Summary(upto_para=7, text="Old note.")]
    assert recap.scoped(book, 3, "book")["text"] == ""
    assert recap.scoped(book, 8, "book")["text"] == "Old note."


def test_novel_recap_never_goes_beyond_the_reader_even_with_a_section():
    book = novel()
    for para in range(10):
        for scope in ("chapter", "book"):
            for section in (None, "s1", "s2"):
                r = recap.scoped(book, para, scope, section=section)
                assert r["upto_para"] is None or r["upto_para"] <= para
                if para < 4:
                    assert "Early" not in r["text"] and "Late" not in r["text"]
                if para < 9:
                    assert "Late" not in r["text"]
    assert recap.scoped(book, 6, "chapter", section="s1")["text"] == "Early notes."
    assert recap.scoped(book, 6, "chapter", section="s2")["text"] == ""   # chapter two is not finished yet


def test_novel_with_section_tagged_notes_still_obeys_the_spoiler_rule():
    """A textbook moved to Novels keeps its section notes until re-enriched: still never beyond the reader."""
    book = summarised()
    book.kind = "novel"
    for para in range(12):
        for section in (None, "c1", "c2", "c1-1"):
            for scope in ("chapter", "book"):
                r = recap.scoped(book, para, scope, section=section)
                assert r["upto_para"] is None or r["upto_para"] <= para


def test_recap_endpoint_takes_a_section(data_dir):
    on_disk(data_dir, summarised())
    c = TestClient(main.app)
    r = c.get(f"/api/books/{BOOK_ID}/recap", params={"para": 0, "scope": "chapter", "section": "c2"}).json()
    assert r["text"] == "Chapter two note. It ranks."
    assert c.get(f"/api/books/{BOOK_ID}/recap", params={"para": 0, "scope": "chapter", "section": "x" * 200}).status_code == 422


# ---------- public service path ----------

def public_body() -> dict:
    book = textbook()
    return {"title": book.title, "author": book.author, "kind": "textbook",
            "sections": [{"id": s.id, "title": s.title, "start_para": s.start_para, "level": s.level} for s in book.sections],
            "paragraphs": [{"idx": p.idx, "section_id": p.section_id, "text": p.text} for p in book.paragraphs]}


@pytest.fixture
def served(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", False)
    monkeypatch.setattr(public_api, "_jobs", {})
    monkeypatch.setattr(public_api, "enrich_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "poll_bucket", TokenBucket(10_000, 60))
    monkeypatch.setattr(public_api, "ip_budget", public_api.IpBudget())
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: make_llm())
    return public_api.app


def test_public_api_runs_textbook_summaries_only(served):
    with TestClient(served) as c:
        r = c.post("/api/public/enrich", json=public_body())
        assert r.status_code == 202 and r.json()["estimate_usd"] > 0
        job = r.json()["job_id"]
        for _ in range(400):
            v = c.get(f"/api/public/enrich/{job}").json()
            if v["status"] != "running":
                break
            asyncio.run(asyncio.sleep(0.02))
    assert v["status"] == "done", v
    res = v["result"]
    assert res["characters"] == [] and {s["section_id"] for s in res["summaries"]} == {"c1", "c1-1", "c1-2", "c2"}


def test_public_api_refuses_a_textbook_over_the_cap(served, monkeypatch):
    monkeypatch.setattr(settings, "public_book_cap_usd", 1e-9)
    with TestClient(served) as c:
        r = c.post("/api/public/enrich", json=public_body())
    assert r.status_code == 402 and r.json()["code"] == "book_cap"


async def fast_sleep(_):
    await asyncio.sleep(0.01)


def test_public_client_summarizes_a_textbook_and_validates_the_result(served, data_dir):
    on_disk(data_dir)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=served), base_url="http://public")
    events: list[dict] = []
    ok = asyncio.run(public_client.summarize_textbook(BOOK_ID, lambda **f: events.append(f), client=client, sleep=fast_sleep))
    assert ok and events[-1]["status"] == "done" and events[-1]["message"] == pipeline.TEXTBOOK_DONE
    assert {s.section_id for s in saved(data_dir).summaries} == {"c1", "c1-1", "c1-2", "c2"}


def test_public_client_drops_bad_textbook_summaries(data_dir):
    on_disk(data_dir)

    def handler(req: httpx.Request):
        if req.method == "POST":
            return httpx.Response(202, json={"job_id": "j", "estimate_usd": 0.01})
        return httpx.Response(200, json={"status": "done", "result": {"characters": [], "summaries": [
            {"upto_para": 7, "text": "ok", "section_id": "c1"},
            {"upto_para": 3, "text": "wrong end", "section_id": "c2"},     # c2 ends at 11: corrected
            {"upto_para": 5, "text": "unknown", "section_id": "zz"},       # not a section: dropped
            {"upto_para": 5, "text": "", "section_id": "c1-1"}]}})         # empty: dropped
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://public")
    ok = asyncio.run(public_client.summarize_textbook(BOOK_ID, lambda **f: None, client=client, sleep=fast_sleep))
    got = {s.section_id: s.upto_para for s in saved(data_dir).summaries}
    assert ok and got == {"c1": 7, "c2": 11}


def test_public_client_failure_leaves_the_textbook_readable(data_dir):
    on_disk(data_dir)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={})), base_url="http://public")
    events: list[dict] = []
    ok = asyncio.run(public_client.summarize_textbook(BOOK_ID, lambda **f: events.append(f), client=client, sleep=fast_sleep))
    assert not ok and events[-1]["status"] == "error" and saved(data_dir).summaries == []


# ---------- the script for existing books ----------

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "summarize_textbook.py"


def test_script_prints_the_estimate_and_needs_yes(data_dir):
    on_disk(data_dir)
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib
    mod = importlib.import_module("summarize_textbook")
    calls = []

    async def fake_run(book_id, report, **kw):
        calls.append(book_id)
        return True

    lines: list[str] = []
    assert mod.main(["--book", BOOK_ID], run=fake_run, out=lines.append) == 0
    assert not calls and any("$" in l for l in lines) and any("--yes" in l for l in lines)
    assert mod.main(["--book", BOOK_ID, "--yes"], run=fake_run, out=lines.append) == 0
    assert calls == [BOOK_ID]


def test_script_refuses_over_the_caps_even_with_yes(data_dir, monkeypatch):
    on_disk(data_dir)
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib
    mod = importlib.import_module("summarize_textbook")
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1e-9)
    calls = []

    async def fake_run(book_id, report, **kw):  # pragma: no cover
        calls.append(book_id)
        return True

    lines: list[str] = []
    assert mod.main(["--book", BOOK_ID, "--yes"], run=fake_run, out=lines.append) != 0 and not calls
    assert mod.main(["--book", "u-novel-nope"], run=fake_run, out=lines.append) != 0


def test_script_refuses_a_novel(data_dir):
    on_disk(data_dir, novel())
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib
    mod = importlib.import_module("summarize_textbook")
    lines: list[str] = []
    assert mod.main(["--book", "u-nv-aaaaaaaa", "--yes"], run=None, out=lines.append) != 0


# ---------- every section call failed (review of #133) ----------

class FailAll(FakeLLM):
    async def complete(self, system, user, schema, tier, max_tokens, effort) -> Raw:
        self.calls.append((system, user))
        raise RuntimeError("boom")


OLD_NOTE = Summary(upto_para=7, text="An older note.", section_id="c1")


def test_all_failed_section_calls_report_failure_and_keep_old_notes(data_dir):
    book = textbook()
    book.summaries = [OLD_NOTE]
    on_disk(data_dir, book)
    ok, events = run_textbook(fake=FailAll())
    assert not ok and events[-1]["status"] == "error" and events[-1]["message"] == pipeline.TEXTBOOK_FAILED
    assert saved(data_dir).summaries == [OLD_NOTE]


def test_public_api_textbook_job_with_no_notes_is_an_error(served, monkeypatch):
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: make_llm(FailAll()))
    with TestClient(served) as c:
        job = c.post("/api/public/enrich", json=public_body()).json()["job_id"]
        for _ in range(400):
            v = c.get(f"/api/public/enrich/{job}").json()
            if v["status"] != "running":
                break
            asyncio.run(asyncio.sleep(0.02))
    assert v["status"] == "error" and v["code"] == "failed", v


def test_public_client_empty_notes_is_a_failure_and_keeps_old_notes(data_dir):
    book = textbook()
    book.summaries = [OLD_NOTE]
    on_disk(data_dir, book)

    def handler(req: httpx.Request):
        if req.method == "POST":
            return httpx.Response(202, json={"job_id": "j", "estimate_usd": 0.01})
        return httpx.Response(200, json={"status": "done", "result": {"characters": [], "summaries": []}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://public")
    events: list[dict] = []
    ok = asyncio.run(public_client.summarize_textbook(BOOK_ID, lambda **f: events.append(f), client=client, sleep=fast_sleep))
    assert not ok and events[-1]["status"] == "error" and saved(data_dir).summaries == [OLD_NOTE]
