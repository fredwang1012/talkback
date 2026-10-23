"""Lip-sync for any voice x face combination (#113): the request names the face shown (face_id); the server resolves
it in the caller's own library (404 for anyone else's or an unknown id), falls back to the voice's linked face when
no face_id is sent, and caches by audio + face image. Cloning with an existing face links it instead of copying."""
import json

import pytest

from app import faces, main, voices
from app.config import settings
from tests.test_api import REAL_KEY, env, silent_wav  # noqa: F401  (env is a fixture)
from tests.test_delete_voice import boson  # noqa: F401  (fixture)
from tests.test_faces import KEY, PNG, lipsync_env
from tests.test_per_user import ALICE, BOB, h, pu, token, upload_face  # noqa: F401  (pu is a fixture)

AUDIO = f"/media/tts/{KEY}.mp3"


def render_env(tmp_path, monkeypatch):
    """lipsync_env with a (fake) Boson key set, so the render path runs even where no .env is present."""
    monkeypatch.setattr(settings, "boson_api_key", REAL_KEY)
    return lipsync_env(tmp_path, monkeypatch)
JPG = b"\xff\xd8\xff" + b"fakejpeg"


def test_face_id_lipsyncs_an_uploaded_face_with_a_preset_voice(tmp_path, monkeypatch):
    client, calls = render_env(tmp_path, monkeypatch)
    fid = faces.create_face(JPG, "Ana")["id"]   # a plain upload: bound to no voice
    r = client.post("/api/avatar/talk", json={"audio_url": AUDIO, "voice": "marcus", "face_id": fid})
    assert r.status_code == 200 and r.json()["video_url"].startswith("/media/avatar/")
    assert "/v1/videos" in calls
    # cached by audio + face image: the same pair never renders twice, a different face does
    n = len(calls)
    assert client.post("/api/avatar/stream", json={"audio_url": AUDIO, "voice": "chloe", "face_id": fid}).json() == r.json()
    assert len(calls) == n
    other = faces.create_face(PNG, "Bo")["id"]
    r2 = client.post("/api/avatar/talk", json={"audio_url": AUDIO, "voice": "marcus", "face_id": other})
    assert r2.status_code == 200 and r2.json() != r.json() and len(calls) > n


def test_face_id_also_takes_the_voice_bound_face_and_falls_back_without_it(tmp_path, monkeypatch):
    client, calls = render_env(tmp_path, monkeypatch)
    fid = faces.face_ids_for_voice("voice_abc")[0]
    with_id = client.post("/api/avatar/talk", json={"audio_url": AUDIO, "voice": "voice_abc", "face_id": fid}).json()
    n = len(calls)
    assert client.post("/api/avatar/talk", json={"audio_url": AUDIO, "voice": "voice_abc"}).json() == with_id
    assert len(calls) == n   # the fallback (voice -> its face) hits the same cache entry
    assert client.post("/api/avatar/talk", json={"audio_url": AUDIO, "voice": "marcus"}).status_code == 400


@pytest.mark.parametrize("bad", ["nope", "../x", "", "a" * 81])
def test_unknown_or_malformed_face_id_is_404_before_any_render(tmp_path, monkeypatch, bad):
    client, calls = render_env(tmp_path, monkeypatch)
    for route in ("/api/avatar/talk", "/api/avatar/stream"):
        r = client.post(route, json={"audio_url": AUDIO, "voice": "marcus", "face_id": bad})
        assert r.status_code == 404, (route, bad, r.text)
    assert calls == []


def test_another_users_face_id_is_404(pu):  # noqa: F811
    client, _ = pu
    fid = upload_face(client, ALICE).json()["id"]
    tts = settings.data_dir / "cache" / "tts"
    tts.mkdir(parents=True, exist_ok=True)
    (tts / f"{KEY}.mp3").write_bytes(b"ID3" + b"\0" * 32000)
    body = {"audio_url": AUDIO, "voice": "marcus", "face_id": fid}
    for route in ("/api/avatar/talk", "/api/avatar/stream"):
        assert client.post(route, headers=h(token(BOB)), json=body).status_code == 404
        assert client.post(route, json=body).status_code == 404   # anonymous
    assert client.post("/api/avatar/stream", headers=h(token(ALICE)), json=body).status_code != 404   # her own


def test_clone_with_face_id_links_the_existing_face_without_a_copy(env):  # noqa: F811
    client, _ = env
    fid = client.post("/api/faces", data={"name": "Ana"}, files={"image": ("a.png", PNG, "image/png")}).json()["id"]
    r = client.post("/api/voices/clone", data={"transcript": "hello there", "name": "me", "consent": "true", "face_id": fid},
                    files={"audio": ("a.wav", silent_wav(4), "audio/wav")})
    assert r.status_code == 200, r.text
    assert r.json()["face"] == f"/portraits/faces/{fid}.png"
    listed = client.get("/api/faces").json()
    assert [(f["id"], f["voice"]) for f in listed] == [(fid, "voice_abc")]   # one face, now bound to the voice
    assert client.get("/api/voices/faces").json()["voice_abc"]["image"] == f"/portraits/faces/{fid}.png"
    assert len(list((settings.data_dir / "portraits" / "faces").glob("*.png"))) == 1


def test_clone_with_unknown_face_id_fails_before_calling_boson(env):  # noqa: F811
    client, calls = env
    for bad in ("nope", "../x"):
        r = client.post("/api/voices/clone", data={"transcript": "hello there", "name": "me", "consent": "true", "face_id": bad},
                        files={"audio": ("a.wav", silent_wav(4), "audio/wav")})
        assert r.status_code == 404, r.text
    assert calls["n"] == 0


def test_deleting_a_voice_unlinks_a_linked_face_but_keeps_it(env, boson):  # noqa: F811
    client, _ = env
    fid = client.post("/api/faces", data={"name": "Ana"}, files={"image": ("a.png", PNG, "image/png")}).json()["id"]
    assert client.post("/api/voices/clone", data={"transcript": "hello there", "name": "me", "consent": "true", "face_id": fid},
                       files={"audio": ("a.wav", silent_wav(4), "audio/wav")}).status_code == 200
    r = client.delete("/api/voices/voice_abc")
    assert r.status_code == 200 and r.json()["faces"] == [fid]   # unlinked, not deleted
    assert boson["deleted"] == ["voice_abc"]
    listed = client.get("/api/faces").json()
    assert [(f["id"], f["voice"]) for f in listed] == [(fid, None)]
    assert (settings.data_dir / "portraits" / "faces" / f"{fid}.png").read_bytes() == PNG
    assert client.get("/api/voices/faces").json() == {}
    assert voices.load_voices() == {}
    assert "linked" not in json.loads((settings.data_dir / "faces.json").read_text())[fid]
