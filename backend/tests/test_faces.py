"""Faces for cloned voices: upload with the clone, listing with loops, and lip-synced answer clips."""
import httpx
from fastapi.testclient import TestClient

from app import main, voices
from app.boson import BosonClient
from app.config import settings
from app.ratelimit import TokenBucket
from tests.test_api import clone, env, silent_wav  # noqa: F401  (env is a fixture)

PNG = b"\x89PNG\r\n\x1a\nfake"
KEY = "a" * 64


def test_clone_with_face_is_listed_with_its_loops(env):  # noqa: F811
    client, _ = env
    r = client.post("/api/voices/clone", data={"transcript": "hello there", "name": "me", "consent": "true"},
                    files={"audio": ("a.wav", silent_wav(4), "audio/wav"), "face": ("me.png", PNG, "image/png")})
    assert r.status_code == 200 and r.json()["face"].startswith("/portraits/voices/")
    faces = client.get("/api/voices/faces").json()
    assert faces == {"voice_abc": {"image": r.json()["face"], "talk": None, "idle": None}}
    voices.loop_path("voice_abc", "talk").write_bytes(b"mp4")
    assert client.get("/api/voices/faces").json()["voice_abc"]["talk"].endswith("-talk.mp4")


def test_clone_rejects_a_non_image_face_before_calling_boson(env):  # noqa: F811
    client, calls = env
    r = client.post("/api/voices/clone", data={"transcript": "hello there", "name": "me", "consent": "true"},
                    files={"audio": ("a.wav", silent_wav(4), "audio/wav"), "face": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400 and calls["n"] == 0


def test_save_face_only_for_registered_voices(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    try:
        voices.save_face("voice_nobody", PNG, "png")
        raise AssertionError("expected VoiceInputError")
    except voices.VoiceInputError:
        pass


def lipsync_env(tmp_path, monkeypatch, video_status="completed"):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(main, "avatar_bucket", TokenBucket(1000, 60))
    voices.save_voice("me", "voice_abc")
    voices.save_face("voice_abc", PNG, "png")
    (tmp_path / "cache" / "tts").mkdir(parents=True)
    (tmp_path / "cache" / "tts" / f"{KEY}.mp3").write_bytes(b"ID3" + b"\0" * 32000)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.method == "POST" and req.url.path == "/v1/videos":
            return httpx.Response(200, json={"id": "video_1"})
        if req.url.path == "/v1/videos/video_1":
            return httpx.Response(200, json={"id": "video_1", "status": video_status})
        if req.url.path == "/v1/videos/video_1/content":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "no"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return TestClient(main.app), calls


def test_lipsync_renders_once_and_caches(tmp_path, monkeypatch):
    client, calls = lipsync_env(tmp_path, monkeypatch)
    body = {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "voice_abc"}
    r = client.post("/api/avatar/talk", json=body)
    assert r.status_code == 200 and r.json()["video_url"].startswith("/media/avatar/")
    clip = tmp_path / "cache" / "avatar" / "talk" / r.json()["video_url"].rsplit("/", 1)[1]
    assert clip.read_bytes() == b"MP4DATA"
    n = len(calls)
    assert client.post("/api/avatar/talk", json=body).json() == r.json()
    assert len(calls) == n, "cached clip: Boson is not called again"


def test_lipsync_rejects_foreign_audio_and_faceless_voices(tmp_path, monkeypatch):
    client, calls = lipsync_env(tmp_path, monkeypatch)
    for body in ({"audio_url": "https://evil/x.mp3", "voice": "voice_abc"},
                 {"audio_url": f"/media/tts/../{KEY}.mp3", "voice": "voice_abc"},
                 {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "marcus"}):
        assert client.post("/api/avatar/talk", json=body).status_code == 400
    assert client.post("/api/avatar/talk", json={"audio_url": f"/media/tts/{'b' * 64}.mp3", "voice": "voice_abc"}).status_code == 404
    assert calls == []


def test_lipsync_render_failure_is_a_clean_error(tmp_path, monkeypatch):
    client, _ = lipsync_env(tmp_path, monkeypatch, video_status="failed")
    r = client.post("/api/avatar/talk", json={"audio_url": f"/media/tts/{KEY}.mp3", "voice": "voice_abc"})
    assert r.status_code >= 500 and "MP4" not in r.text
    assert not list((tmp_path / "cache" / "avatar" / "talk").glob("*.mp4"))


def test_voice_and_face_lists_need_the_access_code_when_one_is_set(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "access_token", "s3cret")
    assert client.get("/api/voices").status_code == 401
    assert client.get("/api/voices/faces").status_code == 401
    assert client.get("/api/voices/faces", headers={"X-Talkback-Token": "s3cret"}).status_code == 200
