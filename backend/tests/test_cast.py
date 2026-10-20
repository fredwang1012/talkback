"""Per-book cast settings: defaults, validation, persistence, atomic write, TTS honours it, access gating."""
import json

import pytest

from app import cast, faces, main
from app.config import settings
from app.ratelimit import TokenBucket
from tests.test_api import env  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\nfake"
EMPTY = {"main": ["harry", "king"], "narrator": {"voice": None, "face": None},
         "characters": {"harry": {"voice": None, "face": None}, "king": {"voice": None, "face": None}}}


def test_defaults_for_a_book_without_settings(env):  # noqa: F811
    client, _ = env
    r = client.get("/api/books/demo/cast")
    assert r.status_code == 200 and r.json() == EMPTY
    assert not (settings.data_dir / "books" / "demo" / "cast_settings.json").exists()


def test_unknown_book_404(env):  # noqa: F811
    client, _ = env
    assert client.get("/api/books/nope/cast").status_code == 404
    assert client.put("/api/books/nope/cast", json={}).status_code == 404
    assert client.get("/api/books/..%2Fx/cast").status_code == 404


def test_put_persists_and_round_trips(env):  # noqa: F811
    client, _ = env
    fid = faces.create_face(PNG, "Ana")["id"]
    (settings.data_dir / "voices.json").write_text(json.dumps({"me": "voice_abc"}), encoding="utf-8")
    body = {"main": ["king"], "narrator": {"voice": "nora", "face": fid},
            "characters": {"king": {"voice": "voice_abc", "face": None}}}
    r = client.put("/api/books/demo/cast", json=body)
    assert r.status_code == 200
    expect = {"main": ["king"], "narrator": {"voice": "nora", "face": fid},
              "characters": {"harry": {"voice": None, "face": None}, "king": {"voice": "voice_abc", "face": None}}}
    assert r.json() == expect
    assert client.get("/api/books/demo/cast").json() == expect
    d = settings.data_dir / "books" / "demo"
    assert json.loads((d / "cast_settings.json").read_text(encoding="utf-8"))["main"] == ["king"]
    assert not list(d.glob("*.tmp")), "written via temp file + rename"


def test_put_rejects_bad_values_and_changes_nothing(env):  # noqa: F811
    client, _ = env
    for body in ({"main": ["ghost"]},
                 {"characters": {"ghost": {"voice": "nora"}}},
                 {"narrator": {"voice": "not-a-voice"}},
                 {"characters": {"harry": {"voice": "voice_made_up"}}},
                 {"narrator": {"face": "fdeadbeef"}},
                 {"narrator": {"face": "../etc"}},
                 {"narrator": "nora"}):
        assert client.put("/api/books/demo/cast", json=body).status_code in (400, 422), body
    assert client.get("/api/books/demo/cast").json() == EMPTY
    assert not (settings.data_dir / "books" / "demo" / "cast_settings.json").exists()


def test_main_defaults_skip_characters_marked_minor(env):  # noqa: F811
    client, _ = env
    p = settings.data_dir / "books" / "demo" / "book.json"
    book = json.loads(p.read_text(encoding="utf-8"))
    book["characters"][1]["main"] = False
    p.write_text(json.dumps(book), encoding="utf-8")
    assert client.get("/api/books/demo/cast").json()["main"] == ["harry"]
    assert client.get("/api/books/demo").json()["characters"][0].get("main") is None


def test_tts_uses_the_cast_voice_unless_overridden(env, monkeypatch):  # noqa: F811
    client, _ = env
    assert client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"}).json()["voice"] == "marcus"
    client.put("/api/books/demo/cast", json={"narrator": {"voice": "nora"}})
    assert client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"}).json()["voice"] == "nora"
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "jake"})
    assert r.json()["voice"] == "jake"
    client.put("/api/books/demo/cast", json={"narrator": {"voice": None}})
    assert client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"}).json()["voice"] == "marcus"


def test_a_cast_voice_that_was_removed_falls_back_to_the_book_voice(env):  # noqa: F811
    book = cast.Book.model_validate_json((settings.data_dir / "books" / "demo" / "book.json").read_text(encoding="utf-8"))
    (settings.data_dir / "books" / "demo" / "cast_settings.json").write_text(
        json.dumps({"narrator": {"voice": "voice_gone"}}), encoding="utf-8")
    assert cast.voice_for(book, "narrator") is None


def test_cast_needs_the_access_code(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "access_token", "s3cret")
    assert client.get("/api/books/demo/cast").status_code == 401
    assert client.put("/api/books/demo/cast", json={}).status_code == 401
    h = {"X-Talkback-Token": "s3cret"}
    assert client.get("/api/books/demo/cast", headers=h).status_code == 200
    assert client.put("/api/books/demo/cast", json={}, headers=h).status_code == 200


def test_non_main_characters_cannot_be_talked_to_and_are_not_counted(env):  # noqa: F811
    client, _ = env
    p = settings.data_dir / "books" / "demo" / "book.json"
    book = json.loads(p.read_text(encoding="utf-8"))
    total = len(book["characters"])
    minor = book["characters"][1]["id"]
    book["characters"][1]["main"] = False
    p.write_text(json.dumps(book), encoding="utf-8")
    body = {"book_id": "demo", "query": "x", "reader_para": 0, "speaker_id": minor, "spoilers": True}
    assert client.post("/api/retrieve", json=body).status_code == 403
    assert client.post("/api/answer", json=body).status_code == 403
    assert client.get("/api/books").json()[0]["characters"] == total - 1
    assert next(c for c in client.get("/api/books/demo").json()["characters"] if c["id"] == minor)["main"] is False
