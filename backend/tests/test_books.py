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


def test_portraits_and_cover_are_picked_up_when_the_files_exist(client, tmp_path):
    d = tmp_path / "books" / "demo"
    book = Book.model_validate_json((d / "book.json").read_text(encoding="utf-8"))
    from app.schema import Character, Narrator
    book.characters = [Character(id="harry", name="Harry", first_para=0, voice="oliver", persona="x"),
                       Character(id="pell", name="Pell", first_para=0, voice="marcus", persona="y")]
    book.narrator = Narrator(voice="marcus")
    (d / "book.json").write_text(book.model_dump_json(), encoding="utf-8")
    pdir = tmp_path / "portraits" / "demo"
    pdir.mkdir(parents=True)
    (pdir / "harry.png").write_bytes(b"png")
    (pdir / "narrator.webp").write_bytes(b"webp")
    (pdir / "cover.jpg").write_bytes(b"jpg")
    got = client.get("/api/books/demo").json()
    by_id = {c["id"]: c["portrait"] for c in got["characters"]}
    assert by_id == {"harry": "/portraits/demo/harry.png", "pell": None}
    assert got["narrator"]["portrait"] == "/portraits/demo/narrator.webp"
    assert client.get("/api/books").json()[0]["cover"] == "/portraits/demo/cover.jpg"


def test_avatar_endpoints_list_and_serve_only_existing_clips(client, tmp_path):
    d = tmp_path / "cache" / "avatar" / "demo"
    d.mkdir(parents=True)
    (d / "p3c0.mp4").write_bytes(b"mp4")
    assert client.get("/api/avatars/demo").json() == ["p3c0"]
    assert client.get("/api/avatars/other").json() == []
    ok = client.get("/api/avatar/p3c0", params={"book": "demo"})
    assert ok.status_code == 200 and ok.headers["content-type"] == "video/mp4" and ok.content == b"mp4"
    assert client.get("/api/avatar/p9c9", params={"book": "demo"}).status_code == 404
    for bad in ("..", "a%2F..%2Fb", "x" * 200):
        assert client.get(f"/api/avatar/{bad}", params={"book": "demo"}).status_code == 404
    assert client.get("/api/avatar/p3c0", params={"book": "../demo"}).status_code == 404
