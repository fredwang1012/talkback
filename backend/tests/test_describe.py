"""Title and author for the Add book form (#83): PDF metadata first, then one cheap LLM call on the opening pages.
The LLM is always a fake here: no live calls."""
import asyncio

import pymupdf as fitz
import pytest

from app import deps
from app.config import settings
from app.enrich import describe
from app.ratelimit import TokenBucket
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)


def pdf(pages: list[str], title: str = "", author: str = "") -> bytes:
    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_textbox(fitz.Rect(72, 72, 520, 760), text, fontsize=11)
    doc.set_metadata({"title": title, "author": author})
    return doc.tobytes()


class FakeLLM:
    """Stands in for Gemini: answers the describe schema with a title and a JSON list of authors."""
    def __init__(self, title="", author: str | list[str] = "", fail=False):
        authors = author if isinstance(author, list) else [author] if author else []
        self.calls, self.answer, self.fail = [], (title, authors), fail

    async def complete_json(self, system, user, schema_model, tier="fast", **kw):
        self.calls.append(user)
        if self.fail:
            raise RuntimeError("boom")
        return schema_model(title=self.answer[0], authors=self.answer[1])


@pytest.mark.parametrize("title, junk", [
    ("Pride and Prejudice", False), ("", True), ("Untitled", True), ("Microsoft Word - draft3.docx", True),
    ("chapter1.pdf", True), ("Document1", True), ("123", True), ("untitled document", True), ("A", True),
])
def test_junk_titles(title, junk):
    assert describe.junk_title(title) is junk


@pytest.mark.parametrize("author, junk", [("Jane Austen", False), ("", True), ("Administrator", True),
                                          ("user", True), ("Owner", True), ("Adobe Acrobat", True)])
def test_junk_authors(author, junk):
    assert describe.junk_author(author) is junk


@pytest.mark.parametrize("raw, joined", [
    ("Jane Austen", "Jane Austen"),
    ("Christopher D. Manning; Prabhakar Raghavan; Hinrich Schütze", "Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze"),
    ("Christopher D. Manning and Prabhakar Raghavan and Hinrich Schütze", "Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze"),
    ("Christopher D. Manning, Prabhakar Raghavan, and Hinrich Schütze", "Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze"),
    ("Brian W. Kernighan & Dennis M. Ritchie", "Brian W. Kernighan, Dennis M. Ritchie"),
    ("Abelson, Harold; Sussman, Gerald Jay", "Harold Abelson, Gerald Jay Sussman"),   # "Last, First" is turned round
    ("Austen, Jane", "Jane Austen"),
    ("Smith, J., Jones, K.", "J. Smith, K. Jones"),
    ("Martin Luther King, Jr.", "Martin Luther King, Jr."),   # not two names: kept as written
    ("Jane Austen;  ;Jane Austen", "Jane Austen"),   # empties and repeats dropped
    ("  Lewis   Carroll ", "Lewis Carroll"),
])
def test_metadata_authors_are_split_on_separators_and_joined_with_commas(raw, joined):
    assert describe.join_authors(describe.split_authors(raw)) == joined


def test_join_authors_drops_junk_and_caps_the_length():
    assert describe.join_authors(["Ada Lovelace", "", "  ", "Administrator", "ada lovelace", "Charles Babbage"]) == \
        "Ada Lovelace, Charles Babbage"
    assert len(describe.join_authors(["x" * 80, "y" * 80])) == describe.MAX_FIELD


def test_names_run_together_in_metadata_are_split_by_asking_the_llm_for_a_list():
    # the IR book's PDF metadata: three authors with only spaces between them (#95)
    llm = FakeLLM("Introduction to Information Retrieval", ["Christopher D. Manning", "Prabhakar Raghavan", "Hinrich Schütze"])
    data = pdf(["Introduction to Information Retrieval\nChristopher D. Manning\nPrabhakar Raghavan\nHinrich Schütze"],
               "Introduction to Information Retrieval", "Christopher D. Manning Prabhakar Raghavan Hinrich Schütze")
    out = asyncio.run(describe.describe(data, ".pdf", llm=llm))
    assert out == {"title": "Introduction to Information Retrieval",
                   "author": "Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze", "source": "llm"}
    assert len(llm.calls) == 1


def test_run_together_names_stay_as_they_were_when_the_llm_cannot_help():
    data = pdf(["x"], "Introduction to Information Retrieval", "Christopher D. Manning Prabhakar Raghavan Hinrich Schütze")
    out = asyncio.run(describe.describe(data, ".pdf", llm=FakeLLM(fail=True)))
    assert out["author"] == "Christopher D. Manning Prabhakar Raghavan Hinrich Schütze" and out["source"] == "metadata"


def test_separated_metadata_authors_need_no_llm():
    llm = FakeLLM("Wrong", "Wrong")
    data = pdf(["x"], "Introduction to Information Retrieval", "Christopher D. Manning; Prabhakar Raghavan; Hinrich Schütze")
    out = asyncio.run(describe.describe(data, ".pdf", llm=llm))
    assert out["author"] == "Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze" and llm.calls == []


def test_the_llm_list_of_authors_is_joined_with_commas():
    llm = FakeLLM("Structure and Interpretation of Computer Programs", ["Harold Abelson", "Gerald Jay Sussman", "Julie Sussman"])
    out = asyncio.run(describe.describe(pdf(["SICP"]), ".pdf", llm=llm))
    assert out["author"] == "Harold Abelson, Gerald Jay Sussman, Julie Sussman"


def test_a_gutenberg_author_line_with_and_is_joined_with_commas():
    txt = b"Title: Good Omens\n\nAuthor: Terry Pratchett and Neil Gaiman\n\n..."
    out = asyncio.run(describe.describe(txt, ".txt", llm=FakeLLM("x", "y")))
    assert out["author"] == "Terry Pratchett, Neil Gaiman"


def test_good_metadata_needs_no_llm():
    llm = FakeLLM("Wrong", "Wrong")
    out = asyncio.run(describe.describe(pdf(["Some text."], "Pride and Prejudice", "Jane Austen"), ".pdf", llm=llm))
    assert out == {"title": "Pride and Prejudice", "author": "Jane Austen", "source": "metadata"}
    assert llm.calls == []


def test_junk_metadata_asks_the_llm_with_the_first_pages_only():
    pages = ["THE TIME MACHINE\n\nby H. G. Wells", "Page two text. " * 50, "Page three. " * 50, "PAGE FOUR SECRET"]
    llm = FakeLLM("The Time Machine", "H. G. Wells")
    out = asyncio.run(describe.describe(pdf(pages, "Microsoft Word - tm.doc", "Administrator"), ".pdf", llm=llm))
    assert out == {"title": "The Time Machine", "author": "H. G. Wells", "source": "llm"}
    assert len(llm.calls) == 1
    sent = llm.calls[0]
    assert "THE TIME MACHINE" in sent and "PAGE FOUR SECRET" not in sent
    assert len(sent) <= describe.MAX_CHARS + 200


def test_the_text_sent_is_capped():
    llm = FakeLLM("T", "A")
    asyncio.run(describe.describe(pdf(["word " * 500] * 3), ".pdf", llm=llm))
    assert len(llm.calls[0]) <= describe.MAX_CHARS + 200


def test_a_good_metadata_title_is_kept_and_only_the_author_comes_from_the_llm():
    llm = FakeLLM("Something Else", "Mary Shelley")
    out = asyncio.run(describe.describe(pdf(["Frankenstein"], "Frankenstein", ""), ".pdf", llm=llm))
    assert out["title"] == "Frankenstein" and out["author"] == "Mary Shelley"


def test_llm_failure_falls_back_to_what_metadata_had():
    out = asyncio.run(describe.describe(pdf(["x"], "Untitled", "Jane Austen"), ".pdf", llm=FakeLLM(fail=True)))
    assert out == {"title": "", "author": "Jane Austen", "source": "metadata"}


def test_no_llm_when_enrichment_is_off(monkeypatch):
    monkeypatch.setattr(settings, "upload_enrich", False)
    monkeypatch.setattr(describe, "make_llm", lambda: pytest.fail("no LLM may be made"))
    out = asyncio.run(describe.describe(pdf(["x"]), ".pdf"))
    assert out == {"title": "", "author": "", "source": "none"}


def test_gutenberg_text_headers_need_no_llm():
    txt = b"The Project Gutenberg eBook of Alice\n\nTitle: Alice's Adventures in Wonderland\n\nAuthor: Lewis Carroll\n\n..."
    llm = FakeLLM("x", "y")
    out = asyncio.run(describe.describe(txt, ".txt", llm=llm))
    assert out == {"title": "Alice's Adventures in Wonderland", "author": "Lewis Carroll", "source": "metadata"}
    assert llm.calls == []


def test_plain_text_sends_its_first_lines():
    txt = ("THE GARDEN PARTY\nKatherine Mansfield\n\n" + "And after all the weather was ideal. " * 400).encode()
    llm = FakeLLM("The Garden Party", "Katherine Mansfield")
    out = asyncio.run(describe.describe(txt, ".txt", llm=llm))
    assert out["title"] == "The Garden Party" and out["source"] == "llm"
    assert llm.calls[0].startswith("THE GARDEN PARTY") and len(llm.calls[0]) <= describe.MAX_CHARS + 200


def test_llm_answers_are_cleaned():
    llm = FakeLLM("  The   Hobbit\n", "x" * 300)
    out = asyncio.run(describe.describe(pdf(["x"]), ".pdf", llm=llm))
    assert out["title"] == "The Hobbit" and len(out["author"]) == 120


def test_endpoint(client, monkeypatch):
    llm = FakeLLM("The Time Machine", "H. G. Wells")
    monkeypatch.setattr(describe, "make_llm", lambda: llm)
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(describe.spend, "add", lambda *a, **k: 0.0)
    r = client.post("/api/books/describe", files={"file": ("tm.pdf", pdf(["THE TIME MACHINE"]))})
    assert r.status_code == 200
    assert r.json() == {"title": "The Time Machine", "author": "H. G. Wells", "source": "llm"}
    assert not (settings.data_dir / "books").exists() or not any((settings.data_dir / "books").iterdir())  # nothing kept


def test_endpoint_validation_gating_and_rate_limit(client, monkeypatch):
    assert client.post("/api/books/describe", files={"file": ("a.docx", b"x")}).status_code == 400
    assert client.post("/api/books/describe", files={"file": ("a.pdf", b"not a pdf")}).status_code == 400
    monkeypatch.setattr(deps, "describe_bucket", TokenBucket(1, 60))
    assert client.post("/api/books/describe", files={"file": ("a.txt", b"Hello")}).status_code == 200
    assert client.post("/api/books/describe", files={"file": ("a.txt", b"Hello")}).status_code == 429
    monkeypatch.setattr(settings, "access_token", "sesame")
    assert client.post("/api/books/describe", files={"file": ("a.txt", b"Hello")}).status_code == 401


def test_a_broken_pdf_describes_as_empty(client):
    r = client.post("/api/books/describe", files={"file": ("a.pdf", b"%PDF-1.4 garbage")})
    assert r.status_code == 200 and r.json() == {"title": "", "author": "", "source": "none"}


# ---- hardening (security review): cost caps, account, bounded parsing ----

from tests.test_api import env  # noqa: E402,F401
from tests.test_per_user import ALICE, h, pu, token  # noqa: E402,F401  (pu is a fixture)


@pytest.fixture
def llm_on(monkeypatch):
    llm = FakeLLM("The Time Machine", "H. G. Wells")
    added = []
    monkeypatch.setattr(describe, "make_llm", lambda: llm)
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(describe.spend, "add", lambda book, provider, usd, **k: added.append(usd) or 0.0)
    monkeypatch.setattr(describe.spend, "total_usd", lambda: 0.0)
    monkeypatch.setattr(describe, "_llm_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(describe, "_daily", {"day": "", "n": 0})
    return llm, added


def test_anonymous_callers_need_an_account_under_per_user(pu):
    client, _ = pu
    send = lambda **kw: client.post("/api/books/describe", files={"file": ("a.txt", b"Hello")}, **kw)  # noqa: E731
    assert send().status_code == 403
    assert send(headers=h(token(ALICE))).status_code == 200


def test_the_estimate_is_reserved_before_the_call(llm_on):
    llm, added = llm_on
    seen = []
    real = llm.complete_json

    async def spy(*a, **k):
        seen.append(list(added))   # what was reserved when the call started
        return await real(*a, **k)

    llm.complete_json = spy
    asyncio.run(describe.describe(pdf(["THE TIME MACHINE"]), ".pdf"))
    assert seen and seen[0] and seen[0][0] > 0


def test_no_call_when_the_budget_cannot_cover_the_estimate(llm_on, monkeypatch):
    llm, added = llm_on
    monkeypatch.setattr(describe.spend, "total_usd", lambda: settings.upload_total_cap_usd - 1e-9)
    out = asyncio.run(describe.describe(pdf(["THE TIME MACHINE"]), ".pdf"))
    assert out["source"] == "none" and llm.calls == [] and added == []


def test_a_daily_cap_on_llm_calls(llm_on, monkeypatch):
    llm, _ = llm_on
    monkeypatch.setattr(describe, "DAILY_LLM_CALLS", 2)
    for _ in range(4):
        asyncio.run(describe.describe(pdf(["THE TIME MACHINE"]), ".pdf"))
    assert len(llm.calls) == 2


def test_a_global_llm_bucket(llm_on, monkeypatch):
    llm, _ = llm_on
    monkeypatch.setattr(describe, "_llm_bucket", TokenBucket(1, 60))
    for _ in range(3):
        asyncio.run(describe.describe(pdf(["THE TIME MACHINE"]), ".pdf"))
    assert len(llm.calls) == 1   # whoever sends them, across every account


def test_parsing_is_bounded_and_full_means_429(client, monkeypatch):
    monkeypatch.setattr(describe, "_parsing", describe.PARSE_CONCURRENCY)
    r = client.post("/api/books/describe", files={"file": ("a.txt", b"Hello")})
    assert r.status_code == 429


def test_a_slow_parse_times_out_and_keeps_its_slot_until_the_thread_ends(monkeypatch):
    import time as _time
    monkeypatch.setattr(describe, "PARSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(describe, "parts", lambda data, ext: (_time.sleep(0.3), ("T", "A", ""))[1])

    async def run():
        out = await describe.describe(b"x", ".txt")
        busy_after_timeout = describe._parsing
        await asyncio.sleep(0.4)
        return out, busy_after_timeout, describe._parsing

    out, busy, after = asyncio.run(run())
    assert out == {"title": "", "author": "", "source": "none"}
    assert busy == 1 and after == 0   # a thread cannot be killed: its slot is freed when it really ends


def test_only_the_start_of_a_text_file_is_read(client, monkeypatch):
    got = []

    async def fake(data, ext, **k):
        got.append(len(data))
        return {"title": "", "author": "", "source": "none"}

    monkeypatch.setattr(describe, "describe", fake)
    client.post("/api/books/describe", files={"file": ("a.txt", b"x" * 2_000_000)})
    assert got == [describe.TEXT_PREFIX_BYTES]
