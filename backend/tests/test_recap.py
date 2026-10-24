"""GET /api/books/{id}/recap: the reader's "Previously...", never from beyond their place."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.schema import Book, Paragraph, Section, Summary


def recap_book():
    return Book(
        id="demo", title="Demo", author="A", kind="novel", license="PD",
        sections=[Section(id="s1", title="Chapter One", start_para=2), Section(id="s2", title="Chapter Two", start_para=6)],
        paragraphs=[Paragraph(idx=i, section_id="s1" if i < 6 else "s2", text=f"Paragraph {i}.") for i in range(10)],
        summaries=[Summary(upto_para=3, text="Early notes."), Summary(upto_para=7, text="Later notes.")],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    d = tmp_path / "books" / "demo"
    d.mkdir(parents=True)
    (d / "book.json").write_text(recap_book().model_dump_json(), encoding="utf-8")
    return TestClient(main.app)


def test_never_returns_a_summary_from_beyond_the_reader(client):
    for para in range(10):
        j = client.get("/api/books/demo/recap", params={"para": para}).json()
        assert j["upto_para"] is None or j["upto_para"] <= para
        assert j["text"] == {3: "Early notes.", 7: "Later notes."}.get(j["upto_para"], "")
    assert client.get("/api/books/demo/recap", params={"para": 5}).json()["text"] == "Early notes."
    assert client.get("/api/books/demo/recap", params={"para": 7}).json()["text"] == "Later notes."


def test_para_beyond_the_end_is_clamped(client):
    j = client.get("/api/books/demo/recap", params={"para": 9999}).json()
    assert j["upto_para"] == 7 and j["chapter"] == "Chapter Two"
    assert client.get("/api/books/demo/recap", params={"para": -5}).json()["text"] == ""


def test_needs_the_access_code_when_one_is_set(client, monkeypatch):
    monkeypatch.setattr(settings, "access_token", "sesame")
    assert client.get("/api/books/demo/recap", params={"para": 5}).status_code == 401
    r = client.get("/api/books/demo/recap", params={"para": 5}, headers={"X-Talkback-Token": "sesame"})
    assert r.status_code == 200 and r.json()["text"] == "Early notes."


def test_empty_before_the_first_summary(client):
    j = client.get("/api/books/demo/recap", params={"para": 1}).json()
    assert j == {"upto_para": None, "chapter": "", "text": "", "earlier": "", "earlier_full": ""}
    assert client.get("/api/books/demo/recap", params={"para": 2}).json()["chapter"] == "Chapter One"


def test_unknown_book_is_404(client):
    assert client.get("/api/books/nope/recap").status_code == 404


# ---- scopes (#89): last chapter / this book so far / the series so far ----

from app import recap  # noqa: E402

MARK = "The current chapter so far:"


def chapters_book(summaries, n=30, series=None, idx=None, bid="demo"):
    """Three chapters: I = 0..9, II = 10..19, III = 20..29."""
    return Book(id=bid, title=f"Book {idx or ''}".strip(), author="A", kind="novel", license="PD",
                sections=[Section(id=f"s{i}", title=t, start_para=p) for i, (t, p) in enumerate([("I", 0), ("II", 10), ("III", 20)])],
                paragraphs=[Paragraph(idx=i, section_id="s0", text=f"P{i}.") for i in range(n)],
                summaries=summaries, series=series, series_index=idx)


MARKED = [Summary(upto_para=5, text="Ann met Bob at the fair."), Summary(upto_para=9, text="Ann met Bob at the fair and they danced."),
          Summary(upto_para=15, text=f"Ann met Bob.\n\n{MARK} Bob sailed to Spain."),
          Summary(upto_para=19, text=f"Ann met Bob.\n\n{MARK} Bob sailed to Spain and found gold."),
          Summary(upto_para=25, text=f"Ann met Bob; Bob found gold.\n\n{MARK} Ann wrote to Bob.")]


def test_last_chapter_is_the_one_just_finished_and_only_its_part():
    b = chapters_book(MARKED)
    r = recap.scoped(b, 22, "chapter")
    assert r["chapter"] == "II" and r["upto_para"] == 19
    assert r["text"] == "Bob sailed to Spain and found gold."


def test_last_chapter_in_the_first_chapter_is_that_chapter_so_far():
    b = chapters_book(MARKED)
    r = recap.scoped(b, 7, "chapter")
    assert r["chapter"] == "I" and r["upto_para"] == 5 and r["text"] == "Ann met Bob at the fair."
    assert recap.scoped(b, 3, "chapter")["text"] == ""  # nothing summarised yet


def test_last_chapter_from_rolling_notes_keeps_only_the_new_sentences():
    rolling = [Summary(upto_para=9, text="Ann met Bob at the summer fair in town. They danced all evening long."),
               Summary(upto_para=19, text="Ann met Bob at the summer fair in town. They danced all evening long. "
                                          "Mr. Bob later sailed away to Spain on a merchant ship. He found gold in the hills.")]
    r = recap.scoped(chapters_book(rolling), 25, "chapter")
    assert r["chapter"] == "II"
    assert r["text"] == "Mr. Bob later sailed away to Spain on a merchant ship. He found gold in the hills."


def test_book_scope_is_the_latest_summary_up_to_the_reader():
    b = chapters_book(MARKED)
    assert recap.scoped(b, 18, "book")["text"] == MARKED[2].text
    assert recap.scoped(b, 18, "book")["upto_para"] == 15


def test_no_scope_ever_uses_a_summary_beyond_the_reader():
    b = chapters_book(MARKED)
    later = {s.upto_para: s.text for s in MARKED}
    for para in range(30):
        for scope in ("chapter", "book", "series"):
            r = recap.scoped(b, para, scope)
            assert r["upto_para"] is None or r["upto_para"] <= para
            for up, text in later.items():
                if up > para:  # no text from a later summary leaks in
                    own = text.split(MARK)[-1].strip()
                    assert own not in r["text"] or any(own in t for u, t in later.items() if u <= para)


def test_series_scope_is_earlier_books_then_this_book_so_far(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for b in (chapters_book([Summary(upto_para=29, text="One ended.")], series="saga", idx=1, bid="b1"),
              chapters_book(MARKED, series="saga", idx=2, bid="b2")):
        d = tmp_path / "books" / b.id
        d.mkdir(parents=True)
        (d / "book.json").write_text(b.model_dump_json(), encoding="utf-8")
    from app import books as books_mod
    r = recap.scoped(books_mod.load_book("b2"), 12, "series")
    assert r["text"].startswith("Book 1: One ended.")
    assert "This book so far: Ann met Bob at the fair and they danced." in r["text"]
    assert "Spain" not in r["text"]  # the note at 15 is beyond the reader
    first = recap.scoped(books_mod.load_book("b1"), 29, "series")
    assert first["text"] == "This book so far: One ended."  # no earlier books


def test_endpoint_takes_a_scope_and_rejects_unknown_ones(client):
    j = client.get("/api/books/demo/recap", params={"para": 9, "scope": "chapter"}).json()
    assert j["scope"] == "chapter" and j["chapter"] == "Chapter One" and j["text"] == "Early notes."
    assert client.get("/api/books/demo/recap", params={"para": 9, "scope": "book"}).json()["text"] == "Later notes."
    assert client.get("/api/books/demo/recap", params={"para": 9, "scope": "series"}).json()["text"] == "This book so far: Later notes."
    assert client.get("/api/books/demo/recap", params={"para": 9, "scope": "nope"}).status_code == 422
