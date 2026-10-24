"""Built-in books (classics, hp*) are hidden per account instead of deleted: their files are never touched."""
from app import users
from app.config import settings
from tests.test_per_user import ALICE, BOB, OWNER, h, ids, pu, token  # noqa: F401  (pu is a fixture)
from tests.test_api import env  # noqa: F401
from tests.test_upload import client, post  # noqa: F401  (client is a fixture)


def listed(client, **kw):
    return {b["id"] for b in client.get("/api/books", **kw).json()}


def test_hide_and_restore_a_builtin_book(client, tmp_path):
    (tmp_path / "books" / "demo").mkdir(parents=True)
    from tests.test_api import book_with_chunk
    (tmp_path / "books" / "demo" / "book.json").write_text(book_with_chunk().model_dump_json(), encoding="utf-8")
    before = (tmp_path / "books" / "demo" / "book.json").read_bytes()
    assert "demo" in listed(client)

    r = client.post("/api/books/demo/hide")
    assert r.status_code == 200 and r.json() == {"hidden": "demo"}
    assert "demo" not in listed(client)
    assert (tmp_path / "books" / "demo" / "book.json").read_bytes() == before   # never touched on disk
    hidden = client.get("/api/hidden-books").json()
    assert [b["id"] for b in hidden] == ["demo"] and hidden[0]["title"]
    assert client.post("/api/books/demo/hide").status_code == 200   # idempotent
    assert len(client.get("/api/hidden-books").json()) == 1

    r = client.post("/api/books/demo/restore")
    assert r.status_code == 200 and r.json() == {"restored": "demo"}
    assert "demo" in listed(client)
    assert client.get("/api/hidden-books").json() == []
    assert client.post("/api/books/demo/restore").status_code == 200   # idempotent


def test_hidden_list_lives_next_to_the_users_data_locally(client, tmp_path):
    (tmp_path / "books" / "demo").mkdir(parents=True)
    from tests.test_api import book_with_chunk
    (tmp_path / "books" / "demo" / "book.json").write_text(book_with_chunk().model_dump_json(), encoding="utf-8")
    client.post("/api/books/demo/hide")
    assert (tmp_path / "hidden_books.json").is_file()


def test_uploads_are_deleted_not_hidden_and_unknown_ids_404(client, tmp_path):
    r = post(client, "a.txt", b"One para.\n\nTwo para.", kind="textbook", title="Mine")
    bid = r.json()["book_id"]
    assert client.post(f"/api/books/{bid}/hide").status_code == 403
    assert client.post("/api/books/nope/hide").status_code == 404
    assert client.post("/api/books/..%2Fx/hide").status_code in (404, 405)


def test_hidden_is_per_account(pu):
    client, _ = pu
    a, b = h(token(ALICE)), h(token(BOB))
    assert client.post("/api/books/demo/hide", headers=a).status_code == 200
    assert "demo" not in ids(client.get("/api/books", headers=a))
    assert "demo" in ids(client.get("/api/books", headers=b))   # Bob still sees it
    assert "demo" in ids(client.get("/api/books"))              # and so do anonymous visitors
    root = users.user_root(users.Identity(ALICE, users.ns_for(ALICE)))
    assert (root / "hidden_books.json").is_file()
    assert (settings.data_dir / "books" / "demo" / "book.json").exists()


def test_anonymous_cannot_hide_for_everyone(pu):
    client, _ = pu
    assert client.post("/api/books/demo/hide").status_code == 403
    assert "demo" in ids(client.get("/api/books"))


def test_cannot_hide_a_book_you_cannot_see(pu):
    client, _ = pu
    assert client.post("/api/books/hp1/hide", headers=h(token(ALICE))).status_code == 404   # owner-only book
    assert client.post("/api/books/hp1/hide", headers=h(token(OWNER))).status_code == 200
    assert "hp1" not in ids(client.get("/api/books", headers=h(token(OWNER))))


def test_corrupt_hidden_file_is_ignored(client, tmp_path):
    (tmp_path / "hidden_books.json").write_text("{not json")
    assert client.get("/api/hidden-books").json() == []
    assert client.get("/api/books").status_code == 200
