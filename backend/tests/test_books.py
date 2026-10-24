import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.schema import Book, Paragraph, Section


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    d = tmp_path / "books" / "demo"
    d.mkdir(parents=True)
    book = Book(id="demo", title="Demo", author="A", kind="novel", license="PD",
                sections=[Section(id="s1", title="I.", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text="Hello there.")])
    (d / "book.json").write_text(book.model_dump_json(), encoding="utf-8")
    (tmp_path / "secret.json").write_text("{}")
    return TestClient(app)


def test_list_books(client):
    r = client.get("/api/books")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "demo" and r.json()[0]["paragraphs"] == 1


def test_get_book(client):
    r = client.get("/api/books/demo")
    assert r.status_code == 200 and r.json()["paragraphs"][0]["text"] == "Hello there."


@pytest.mark.parametrize("bad", ["missing", "..", "..%2Fsecret", "Demo", "demo.json"])
def test_unknown_or_unsafe_ids_404(client, bad):
    assert client.get(f"/api/books/{bad}").status_code == 404
