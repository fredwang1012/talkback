"""Per-account separation (PER_USER): IAP JWT verification, namespaces, cross-user 404s, faces/voices isolation,
owner-only books, migration, and the PER_USER=false regression."""
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from app import faces, main, users, voices
from app.config import settings
from app.ratelimit import TokenBucket
from tests.test_api import env  # noqa: F401  (fixture)

AUD = "/projects/272822965835/locations/us-central1/services/talkback"
OWNER, ALICE, BOB = "owner@example.com", "alice@example.com", "bob@example.com"
PNG = b"\x89PNG\r\n\x1a\nfake"

_key = ec.generate_private_key(ec.SECP256R1())
_PRIV = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
_PUB = _key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
_OTHER = ec.generate_private_key(ec.SECP256R1())


class _Resp:
    status = 200

    def __init__(self, data: bytes):
        self.data = data


def token(email=ALICE, aud=AUD, exp_in=600, iss=users.IAP_ISSUER, signer=None):
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "email": email, "iat": now - 5, "exp": now + exp_in}
    s = signer or crypt.ES256Signer.from_string(_PRIV, "k1")
    return jwt.encode(s, payload).decode()


def h(tok):
    return {users.IAP_HEADER: tok}


@pytest.fixture
def pu(env, monkeypatch):  # noqa: F811
    """PER_USER on, IAP certs served from a fake transport; books: demo (public), hp1 (owner-only)."""
    client, calls = env
    monkeypatch.setattr(settings, "per_user", True)
    monkeypatch.setattr(settings, "iap_audience", AUD)
    monkeypatch.setattr(settings, "owner_emails", OWNER)
    monkeypatch.setattr(settings, "owner_only_books", "hp*")
    fetched = []

    def transport(url, method="GET", **kw):
        fetched.append(url)
        return _Resp(json.dumps({"k1": _PUB}).encode())
    monkeypatch.setattr(users, "_transport", lambda: transport)
    users._results.clear()
    d = settings.data_dir / "books"
    book = main.books.load_book("demo")
    for bid in ("hp1", "u-alice-1", "u-bob-1"):
        (d / bid).mkdir()
        (d / bid / "book.json").write_text(book.model_copy(update={"id": bid}).model_dump_json(), encoding="utf-8")
    (d / "u-alice-1" / "owner.txt").write_text(users.ns_for(ALICE))
    (d / "u-bob-1" / "owner.txt").write_text(users.ns_for(BOB))
    for b in ("upload_bucket", "face_bucket", "answer_bucket", "clone_bucket"):
        monkeypatch.setattr(main, b, TokenBucket(1000, 60))
    main._index_for.cache_clear()
    return client, fetched


def ids(r):
    return {b["id"] for b in r.json()}


# ---- JWT verification ----

def test_valid_token_gives_email(pu):
    client, _ = pu
    assert client.get("/api/me", headers=h(token())).json() == {"email": ALICE, "per_user": True}


@pytest.mark.parametrize("tok", ["", "garbage", "a.b.c"])
def test_missing_or_garbage_is_anonymous(pu, tok):
    client, _ = pu
    assert client.get("/api/me", headers=h(tok)).json() == {"email": None, "per_user": True}


def test_expired_wrong_audience_wrong_key_wrong_issuer_rejected(pu):
    client, _ = pu
    bad = [token(exp_in=-3600), token(aud="/projects/1/locations/x/services/y"),
           token(signer=crypt.ES256Signer.from_string(
               _OTHER.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode(), "k1")),
           token(iss="https://evil.example")]
    for t in bad:
        assert client.get("/api/me", headers=h(t)).json()["email"] is None


def test_plain_email_header_is_never_trusted(pu):
    client, _ = pu
    r = client.get("/api/me", headers={"x-goog-authenticated-user-email": f"accounts.google.com:{OWNER}"})
    assert r.json()["email"] is None


def test_results_and_certs_are_cached(pu):
    client, fetched = pu
    t = token()
    for _ in range(3):
        client.get("/api/me", headers=h(t))
    assert len(fetched) == 1


def test_unset_audience_means_everyone_anonymous(pu, monkeypatch):
    client, _ = pu
    monkeypatch.setattr(settings, "iap_audience", "")
    assert client.get("/api/me", headers=h(token())).json()["email"] is None


def test_namespace_derivation():
    assert users.ns_for("A@x.com") == users.ns_for(" a@X.com ")
    assert len(users.ns_for("a@x.com")) == 16 and "@" not in users.ns_for("a@x.com")
    assert users.ns_for("a@x.com") != users.ns_for("b@x.com")


# ---- books ----

def test_library_visibility(pu):
    client, _ = pu
    assert ids(client.get("/api/books", headers=h(token(ALICE)))) == {"demo", "u-alice-1"}
    assert ids(client.get("/api/books", headers=h(token(BOB)))) == {"demo", "u-bob-1"}
    assert ids(client.get("/api/books")) == {"demo"}
    assert ids(client.get("/api/books", headers=h(token(OWNER)))) == {"demo", "hp1"}


BOOK_ROUTES = [
    ("get", "/api/books/{b}", None),
    ("get", "/api/books/{b}/recap", None),
    ("get", "/api/books/{b}/cast", None),
    ("put", "/api/books/{b}/cast", {}),
    ("get", "/api/books/{b}/status", None),
    ("post", "/api/books/{b}/enrich", None),
    ("post", "/api/books/{b}/kind", {"kind": "textbook"}),
    ("post", "/api/retrieve", {"book_id": "{b}", "query": "x", "reader_para": 1}),
    ("post", "/api/answer", {"book_id": "{b}", "query": "x", "reader_para": 1}),
    ("post", "/api/tts", {"book_id": "{b}", "chunk_id": "p0c0"}),
    ("get", "/api/avatars/{b}", None),
    ("get", "/api/avatar/p0c0?book={b}", None),
]


def call(client, method, path, body, bid, headers):
    path = path.format(b=bid)
    kw = {"headers": headers}
    if body is not None:
        kw["json"] = json.loads(json.dumps(body).replace("{b}", bid))
    return getattr(client, method)(path, **kw)


@pytest.mark.parametrize("method,path,body", BOOK_ROUTES)
def test_other_users_upload_is_404_everywhere(pu, method, path, body):
    client, _ = pu
    assert call(client, method, path, body, "u-alice-1", h(token(BOB))).status_code == 404
    assert call(client, method, path, body, "u-alice-1", {}).status_code == 404


@pytest.mark.parametrize("method,path,body", BOOK_ROUTES)
def test_owner_only_book_is_404_for_others_and_anonymous(pu, method, path, body):
    client, _ = pu
    assert call(client, method, path, body, "hp1", h(token(ALICE))).status_code == 404
    assert call(client, method, path, body, "hp1", {}).status_code == 404


def test_owner_reads_owner_only_book_and_user_reads_own(pu):
    client, _ = pu
    assert client.get("/api/books/hp1", headers=h(token(OWNER))).status_code == 200
    assert client.get("/api/books/u-alice-1", headers=h(token(ALICE))).status_code == 200
    assert client.get("/api/books/demo").status_code == 200  # public sample, anonymous


def test_upload_is_claimed_by_the_caller(pu):
    client, _ = pu
    r = client.post("/api/books/upload", headers=h(token(ALICE)), data={"title": "Mine", "kind": "textbook"},
                    files={"file": ("a.txt", b"Hello there.\n\nSecond paragraph here.", "text/plain")})
    assert r.status_code == 202
    bid = r.json()["book_id"]
    assert (settings.data_dir / "books" / bid / "owner.txt").read_text() == users.ns_for(ALICE)
    assert client.get(f"/api/books/{bid}/status", headers=h(token(BOB))).status_code == 404
    assert client.get(f"/api/books/{bid}/status", headers=h(token(ALICE))).status_code == 200


def test_cast_settings_are_per_user(pu):
    client, _ = pu
    a, b = h(token(ALICE)), h(token(BOB))
    assert client.put("/api/books/demo/cast", headers=a, json={"main": ["king"]}).status_code == 200
    assert client.get("/api/books/demo/cast", headers=a).json()["main"] == ["king"]
    assert client.get("/api/books/demo/cast", headers=b).json()["main"] == ["harry", "king"]
    assert not (settings.data_dir / "books" / "demo" / "cast_settings.json").exists()
    assert (settings.data_dir / "users" / users.ns_for(ALICE) / "cast" / "demo.json").is_file()


def test_per_user_rate_key(pu):
    client, _ = pu
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [(users.IAP_HEADER.encode(), token(ALICE).encode())],
                   "client": ("1.2.3.4", 1)})
    assert main.rate_key(req) == f"user:{users.ns_for(ALICE)}"
    anon = Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1)})
    assert main.rate_key(anon) == "1.2.3.4"


# ---- voices and faces ----

def test_voices_isolated(pu):
    client, _ = pu
    voices.save_voice  # noqa: B018
    ns_a = settings.data_dir / "users" / users.ns_for(ALICE)
    ns_a.mkdir(parents=True)
    (ns_a / "voices.json").write_text(json.dumps({"Mine": "voice_alice"}))
    assert client.get("/api/voices", headers=h(token(ALICE))).json() == {"Mine": "voice_alice"}
    assert client.get("/api/voices", headers=h(token(BOB))).json() == {}
    assert client.get("/api/voices").json() == {}
    body = {"voice": "voice_alice", "text": "hello"}
    assert client.post("/api/speak", headers=h(token(BOB)), json=body).status_code == 400   # unknown voice for Bob
    assert client.put("/api/books/demo/cast", headers=h(token(BOB)),
                      json={"narrator": {"voice": "voice_alice"}}).status_code == 400


def upload_face(client, who, name="Ana"):
    return client.post("/api/faces", headers=h(token(who)), data={"name": name}, files={"image": ("a.png", PNG, "image/png")})


def test_faces_isolated(pu):
    client, _ = pu
    f = upload_face(client, ALICE).json()
    a, b = h(token(ALICE)), h(token(BOB))
    assert [x["id"] for x in client.get("/api/faces", headers=a).json()] == [f["id"]]
    assert client.get("/api/faces", headers=b).json() == []
    assert client.get("/api/faces").json() == []
    # use in cast, animate, delete: all 400/404 for the other user, nothing changes
    assert client.put("/api/books/demo/cast", headers=b, json={"narrator": {"face": f["id"]}}).status_code == 400
    assert client.post(f"/api/faces/{f['id']}/animate", headers=b).status_code == 404
    assert client.delete(f"/api/faces/{f['id']}", headers=b).status_code == 404
    assert client.put("/api/books/demo/cast", headers=a, json={"narrator": {"face": f["id"]}}).status_code == 200
    assert (settings.data_dir / "users" / users.ns_for(ALICE) / "faces" / f"{f['id']}.png").is_file()
    assert not (settings.data_dir / "portraits" / "faces").exists()


def test_face_files_are_served_only_to_their_owner(pu):
    client, _ = pu
    f = upload_face(client, ALICE).json()
    assert client.get(f["image"], headers=h(token(ALICE))).content == PNG
    assert client.get(f["image"], headers=h(token(BOB))).status_code == 404
    assert client.get(f["image"]).status_code == 404


def test_shared_portrait_folders_not_served(pu):
    client, _ = pu
    d = settings.data_dir / "portraits"
    (d / "faces").mkdir(parents=True)
    (d / "voices").mkdir()
    (d / "faces" / "old.png").write_bytes(PNG)
    (d / "voices" / "old.png").write_bytes(PNG)
    (d / "hp1").mkdir()
    (d / "hp1" / "cover.png").write_bytes(PNG)
    (d / "demo").mkdir()
    (d / "demo" / "cover.png").write_bytes(PNG)
    for who in (ALICE, OWNER):
        assert client.get("/portraits/faces/old.png", headers=h(token(who))).status_code == 404
        assert client.get("/portraits/voices/old.png", headers=h(token(who))).status_code == 404
    assert client.get("/portraits/hp1/cover.png", headers=h(token(ALICE))).status_code == 404
    assert client.get("/portraits/hp1/cover.png").status_code == 404
    assert client.get("/portraits/hp1/cover.png", headers=h(token(OWNER))).status_code == 200
    assert client.get("/portraits/demo/cover.png").status_code == 200


# ---- migration ----

def seed_shared(root):
    (root / "voices.json").write_text(json.dumps({"Cloned narrator": "voice_x"}))
    (root / "portraits" / "faces").mkdir(parents=True)
    (root / "portraits" / "faces" / "f1.png").write_bytes(PNG)
    (root / "portraits" / "faces" / "f1-talk.mp4").write_bytes(b"mp4")
    (root / "faces.json").write_text(json.dumps({"f1": {"name": "Me", "file": "f1.png", "voice": "voice_x"}}))
    (root / "books" / "demo" / "cast_settings.json").write_text(json.dumps({"main": ["king"]}))
    (root / "books" / "u-old").mkdir()
    (root / "books" / "u-old" / "book.json").write_text(main.books.load_book("demo").model_copy(update={"id": "u-old"}).model_dump_json())


def test_migration_moves_shared_data_to_owner_idempotently(pu):
    client, _ = pu
    root = settings.data_dir
    seed_shared(root)
    acts = users.migrate_to_owner(root, OWNER, dry_run=True)
    assert acts and not (root / "users").exists() and not (root / "books" / "u-old" / "owner.txt").exists()
    users.migrate_on_startup()
    o = root / "users" / users.ns_for(OWNER)
    assert json.loads((o / "voices.json").read_text()) == {"Cloned narrator": "voice_x"}
    assert (o / "faces" / "f1.png").read_bytes() == PNG and (o / "faces" / "f1-talk.mp4").is_file()
    assert (o / "cast" / "demo.json").is_file()
    assert (root / "books" / "u-old" / "owner.txt").read_text() == users.ns_for(OWNER)
    assert (root / "voices.json").is_file() and (root / "faces.json").is_file()   # originals kept
    # the owner now sees them; others do not
    ow = h(token(OWNER))
    assert client.get("/api/voices", headers=ow).json() == {"Cloned narrator": "voice_x"}
    assert [x["name"] for x in client.get("/api/faces", headers=ow).json()] == ["Me"]
    assert client.get("/api/faces", headers=h(token(ALICE))).json() == []
    assert client.get("/api/books/demo/cast", headers=ow).json()["main"] == ["king"]
    assert "u-old" in ids(client.get("/api/books", headers=ow))
    # re-run changes nothing, and never overwrites later edits
    (o / "voices.json").write_text(json.dumps({"New": "voice_y"}))
    assert users.migrate_to_owner(root, OWNER) == []
    assert json.loads((o / "voices.json").read_text()) == {"New": "voice_y"}


def test_migration_never_raises(pu, monkeypatch):
    def boom(*a, **k):
        raise OSError("bucket down")
    monkeypatch.setattr(users, "migrate_to_owner", boom)
    users.migrate_on_startup()


# ---- PER_USER=false regression ----

def test_per_user_off_is_the_shared_namespace(env, monkeypatch):  # noqa: F811
    client, _ = env
    assert settings.per_user is False
    assert client.get("/api/me").json() == {"email": None, "per_user": False}
    # a forged-looking header changes nothing
    r = client.get("/api/me", headers=h("whatever"))
    assert r.json()["email"] is None
    assert {b["id"] for b in client.get("/api/books").json()} == {"demo"}
    monkeypatch.setattr(settings, "owner_only_books", "demo")   # ignored when PER_USER is off
    assert client.get("/api/books/demo").status_code == 200
    f = faces.create_face(PNG, "Ana")
    assert (settings.data_dir / "portraits" / "faces" / f"{f['id']}.png").is_file()
    voices.save_voice("v", "voice_1")
    assert (settings.data_dir / "voices.json").is_file()
    client.put("/api/books/demo/cast", json={"main": ["king"]})
    assert (settings.data_dir / "books" / "demo" / "cast_settings.json").is_file()
    assert not (settings.data_dir / "users").exists()
