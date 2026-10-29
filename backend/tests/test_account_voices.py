"""Voices already on the Boson account (GET /v1/audio/voices): list and import them, owner or local run only; and a
one-off voice preview (ref_audio) that saves nothing."""
import json

import httpx
import pytest

from app import deps
from app.boson import BosonClient
from tests.test_api import env, silent_wav  # noqa: F401  (env is a fixture)
from tests.test_per_user import ALICE, OWNER, h, pu, token  # noqa: F401  (pu is a fixture)


@pytest.fixture
def boson(monkeypatch):
    state = {"speech": []}

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"data": [{"voice_id": "voice_old", "title": "Narrator from last week"},
                                                      {"voice_id": "voice_abc", "title": "Already saved"}]})
        if req.method == "POST" and req.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"voice": "voice_abc", "ref_text": "t"})
        if req.method == "POST" and req.url.path == "/v1/audio/speech":
            state["speech"].append(json.loads(req.content))
            return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "unexpected"}})

    monkeypatch.setattr(deps, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return state


def test_lists_only_unsaved_account_voices_and_imports_one(env, boson):  # noqa: F811
    client, _ = env
    assert client.post("/api/voices/clone", data={"transcript": "hello there", "name": "mine", "consent": "true"},
                       files={"audio": ("a.wav", silent_wav(4), "audio/wav")}).status_code == 200
    r = client.get("/api/voices/account")
    assert r.status_code == 200 and r.json() == [{"id": "voice_old", "title": "Narrator from last week"}]
    r = client.post("/api/voices/import", json={"voice_id": "voice_old", "name": "Old narrator"})
    assert r.status_code == 200
    assert client.get("/api/voices").json() == {"mine": "voice_abc", "Old narrator": "voice_old"}
    assert client.get("/api/voices/account").json() == []


def test_import_refuses_a_voice_not_on_the_account(env, boson):  # noqa: F811
    client, _ = env
    assert client.post("/api/voices/import", json={"voice_id": "voice_someone_else", "name": "x"}).status_code == 404
    assert client.get("/api/voices").json() == {}


def test_account_voices_are_owner_only_when_accounts_are_separate(pu, boson):  # noqa: F811
    client, _ = pu
    assert client.get("/api/voices/account", headers=h(token(ALICE))).status_code == 404
    assert client.post("/api/voices/import", json={"voice_id": "voice_old", "name": "x"}, headers=h(token(ALICE))).status_code == 404
    assert client.get("/api/voices/account", headers=h(token(OWNER))).status_code == 200


def test_preview_speaks_with_the_reference_clip_and_saves_nothing(env, boson):  # noqa: F811
    client, _ = env
    r = client.post("/api/voices/preview", data={"transcript": "hello there", "consent": "true"}, files={"audio": ("a.wav", silent_wav(4), "audio/wav")})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3mp3"
    body = boson["speech"][0]
    assert body["ref_text"] == "hello there" and body["ref_audio"] and "voice" not in body
    assert client.get("/api/voices").json() == {}


def test_preview_checks_the_clip_first(env, boson):  # noqa: F811
    client, _ = env
    r = client.post("/api/voices/preview", data={"transcript": "hi", "consent": "true"}, files={"audio": ("a.wav", silent_wav(1), "audio/wav")})
    assert r.status_code == 400 and boson["speech"] == []
    r = client.post("/api/voices/preview", data={"transcript": "hello there"}, files={"audio": ("a.wav", silent_wav(4), "audio/wav")})
    assert r.status_code == 400 and boson["speech"] == []   # no consent, as for cloning
