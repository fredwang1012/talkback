"""DELETE /api/voices/{id}: Boson delete, saved list, linked face, cast fallback, protected voices, per-user isolation."""
import json

import httpx
import pytest

from app import faces, main, users, voices
from app.boson import BosonClient
from app.config import settings
from tests.test_api import env, silent_wav  # noqa: F401  (env is a fixture)
from tests.test_per_user import ALICE, BOB, h, pu, token  # noqa: F401  (pu is a fixture)

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture
def boson(monkeypatch):
    """Records DELETE /v1/audio/voices/{id}; status chosen per test via state['status']."""
    state = {"deleted": [], "status": 200}

    def handler(req: httpx.Request):
        if req.method == "DELETE" and req.url.path.startswith("/v1/audio/voices/"):
            state["deleted"].append(req.url.path.rsplit("/", 1)[1])
            if state["status"] == 200:
                return httpx.Response(200, json={"deleted": True})
            return httpx.Response(state["status"], json={"error": {"type": "invalid_request_error", "message": "x"}})
        if req.method == "POST" and req.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"voice": "voice_abc", "ref_text": "t"})
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "unexpected"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return state


def clone_with_face(client, name="me"):
    return client.post("/api/voices/clone", data={"transcript": "hello there", "name": name, "consent": "true"},
                       files={"audio": ("a.wav", silent_wav(4), "audio/wav"), "face": ("me.png", PNG, "image/png")})


def seed_clone(client, voice_id="voice_abc"):
    """Clone through the real endpoint with Boson mocked by the env fixture (voice id voice_abc)."""
    assert clone_with_face(client).status_code == 200


def test_delete_removes_voice_keeps_face_and_calls_boson_once(env, boson):  # noqa: F811
    client, _ = env
    seed_clone(client)
    fid = client.get("/api/faces").json()[0]["id"]
    face_files = list((settings.data_dir / "portraits" / "faces").glob(f"{fid}*"))
    assert face_files
    r = client.delete("/api/voices/voice_abc")
    assert r.status_code == 200 and r.json()["faces"] == [fid]
    assert boson["deleted"] == ["voice_abc"]
    assert client.get("/api/voices").json() == {}
    assert [x["id"] for x in client.get("/api/faces").json()] == [fid]   # the face stays, unlinked
    assert client.get("/api/voices/faces").json() == {}
    assert list((settings.data_dir / "portraits" / "faces").glob(f"{fid}*"))
    assert client.delete("/api/voices/voice_abc").status_code == 404   # gone; no second Boson call
    assert boson["deleted"] == ["voice_abc"]


def test_boson_404_means_already_gone(env, boson):  # noqa: F811
    client, _ = env
    seed_clone(client)
    boson["status"] = 404
    assert client.delete("/api/voices/voice_abc").status_code == 200
    assert client.get("/api/voices").json() == {}


def test_boson_failure_keeps_local_data_for_a_retry(env, boson):  # noqa: F811
    client, _ = env
    seed_clone(client)
    boson["status"] = 400
    assert client.delete("/api/voices/voice_abc").status_code >= 400
    assert client.get("/api/voices").json() == {"me": "voice_abc"}
    assert len(client.get("/api/faces").json()) == 1


@pytest.mark.parametrize("vid", ["marcus", "default", "nora"])
def test_presets_are_protected(env, boson, vid):  # noqa: F811
    client, _ = env
    assert client.delete(f"/api/voices/{vid}").status_code == 403
    assert boson["deleted"] == []


def test_unknown_voice_is_404_without_calling_boson(env, boson):  # noqa: F811
    client, _ = env
    assert client.delete("/api/voices/voice_nope").status_code == 404
    assert boson["deleted"] == []


def test_cast_falls_back_to_auto_after_deleting_voice_or_face(env, boson):  # noqa: F811
    client, _ = env
    seed_clone(client)
    d = settings.data_dir / "books" / "u-mine-1"   # an upload (a shipped book's cast would protect the voice)
    d.mkdir()
    (d / "book.json").write_text((settings.data_dir / "books" / "demo" / "book.json").read_text(encoding="utf-8")
                                 .replace('"id":"demo"', '"id":"u-mine-1"'), encoding="utf-8")
    fid = client.get("/api/faces").json()[0]["id"]
    standalone = client.post("/api/faces", data={"name": "Ana"}, files={"image": ("a.png", PNG, "image/png")}).json()["id"]
    body = {"main": ["harry"], "narrator": {"voice": "voice_abc", "tags": None, "face": fid},
            "characters": {"harry": {"voice": "voice_abc", "face": standalone}}}
    assert client.put("/api/books/u-mine-1/cast", json=body).json()["narrator"] == {"voice": "voice_abc", "tags": None, "face": fid}
    client.delete("/api/voices/voice_abc")
    cast = client.get("/api/books/u-mine-1/cast").json()
    assert cast["narrator"] == {"voice": None, "tags": None, "face": fid}   # the face outlives the voice
    assert cast["characters"]["harry"] == {"voice": None, "tags": None, "face": standalone}
    client.delete(f"/api/faces/{standalone}")
    assert client.get("/api/books/u-mine-1/cast").json()["characters"]["harry"] == {"voice": None, "tags": None, "face": None}
    # the round trip the UI does (send the loaded cast back) is accepted
    assert client.put("/api/books/u-mine-1/cast", json=client.get("/api/books/u-mine-1/cast").json()).status_code == 200


def test_voices_are_deleted_per_user(pu, boson):  # noqa: F811
    client, _ = pu
    root = settings.data_dir / "users" / users.ns_for(ALICE)
    root.mkdir(parents=True)
    (root / "voices.json").write_text(json.dumps({"Mine": "voice_alice"}))
    assert client.delete("/api/voices/voice_alice", headers=h(token(BOB))).status_code == 404
    assert client.delete("/api/voices/voice_alice").status_code == 403   # anonymous may not delete at all
    assert boson["deleted"] == []
    assert client.get("/api/voices", headers=h(token(ALICE))).json() == {"Mine": "voice_alice"}
    assert client.delete("/api/voices/voice_alice", headers=h(token(ALICE))).status_code == 200
    assert boson["deleted"] == ["voice_alice"]
    assert client.get("/api/voices", headers=h(token(ALICE))).json() == {}


def test_anonymous_cannot_delete_in_per_user_mode(pu, boson):  # noqa: F811
    client, _ = pu
    shared = settings.data_dir / "users" / users.PUBLIC_NS
    shared.mkdir(parents=True)
    (shared / "voices.json").write_text(json.dumps({"Shared": "voice_pub"}))
    fid = client.post("/api/faces", data={"name": "Pub"}, files={"image": ("a.png", PNG, "image/png")}).json()["id"]
    assert client.delete("/api/voices/voice_pub").status_code == 403
    assert client.delete(f"/api/faces/{fid}").status_code == 403
    assert boson["deleted"] == [] and client.get("/api/voices").json() == {"Shared": "voice_pub"}
    assert len(client.get("/api/faces").json()) == 1
    # a signed-in user can delete their own face
    mine = client.post("/api/faces", headers=h(token(ALICE)), data={"name": "A"}, files={"image": ("a.png", PNG, "image/png")}).json()["id"]
    assert client.delete(f"/api/faces/{mine}", headers=h(token(ALICE))).status_code == 200


def test_local_mode_still_deletes_behind_the_access_code(env, boson, monkeypatch):  # noqa: F811
    client, _ = env
    seed_clone(client)
    monkeypatch.setattr(settings, "access_token", "sekrit")
    assert client.delete("/api/voices/voice_abc").status_code in (401, 403)
    assert client.delete("/api/voices/voice_abc", headers={"x-talkback-token": "sekrit"}).status_code == 200




def test_unmarked_store_entries_and_legacy_files_are_deletable_samples_untouched(env, boson):  # noqa: F811
    client, _ = env
    d = settings.data_dir
    voices.save_voice("Cloned narrator", "voice_narr")        # plain store entries, as on the author's machine
    fid = faces.create_face(PNG, "Me", voice="voice_narr")["id"]
    (d / "portraits" / "voices").mkdir(parents=True)
    for n in ("voice_narr.png", "voice_narr-idle.mp4", "voice_narr-talk.mp4", "other.png", "voice_narrator.png"):
        (d / "portraits" / "voices" / n).write_bytes(b"x")
    (d / "samples" / "person").mkdir(parents=True)
    (d / "samples" / "person" / "voice-clip.wav").write_bytes(b"ref")
    assert client.delete("/api/voices/voice_narr").status_code == 200
    assert boson["deleted"] == ["voice_narr"]
    assert list((d / "portraits" / "faces").glob(f"{fid}*"))   # the face outlives the voice
    assert sorted(p.name for p in (d / "portraits" / "voices").iterdir()) == ["other.png", "voice_narrator.png"]
    assert (d / "samples" / "person" / "voice-clip.wav").read_bytes() == b"ref"


def test_character_portraits_are_not_deletable_as_faces(env):  # noqa: F811
    client, _ = env
    p = settings.data_dir / "portraits" / "demo"
    p.mkdir(parents=True)
    (p / "harry.png").write_bytes(b"x")
    assert client.delete("/api/faces/harry").status_code == 404
    assert client.delete("/api/voices/marcus").status_code == 403
    assert (p / "harry.png").exists()


def test_deleting_a_face_keeps_its_voice(env, boson):  # noqa: F811
    client, _ = env
    seed_clone(client)
    fid = client.get("/api/faces").json()[0]["id"]
    assert client.delete(f"/api/faces/{fid}").status_code == 200
    assert client.get("/api/voices").json() == {"me": "voice_abc"}
    assert boson["deleted"] == []
