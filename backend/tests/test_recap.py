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
