"""POST /api/avatar/stream: proxies Boson's fMP4 stream, tees it into the clip cache, short-circuits cached clips."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import faces, main, voices
from app.boson import BosonClient
from app.config import settings
from app.ratelimit import TokenBucket
from tests.test_faces import KEY, PNG

BODY = {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "voice_abc"}
CHUNKS = [b"\x00\x00\x00\x18ftypiso5", b"moof-1", b"mdat-1", b"moof-2"]


def stream_env(tmp_path, monkeypatch, *, fail_after=None, status=200, headers=None):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(main, "avatar_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr("app.boson.MAX_RETRIES", 0)
    voices.save_voice("me", "voice_abc")
    faces.create_face(PNG, "me", voice="voice_abc")
    (tmp_path / "cache" / "tts").mkdir(parents=True)
    (tmp_path / "cache" / "tts" / f"{KEY}.mp3").write_bytes(b"ID3" + b"\0" * 32000)
    calls = []

    async def gen():
        for i, c in enumerate(CHUNKS):
            if fail_after is not None and i == fail_after:
                raise httpx.ReadError("upstream dropped")
            yield c

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if status != 200:
            return httpx.Response(status, json={"error": {"type": "rate_limit_error", "code": "all_replicas_busy",
                                                          "message": "busy"}}, headers=headers or {})
        return httpx.Response(200, content=gen(), headers={"X-Video-Id": "video_9", "Content-Type": "video/mp4"})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return TestClient(main.app), calls, tmp_path / "cache" / "avatar" / "talk"


def test_stream_forwards_chunks_in_order_and_caches_on_success(tmp_path, monkeypatch):
    client, calls, d = stream_env(tmp_path, monkeypatch)
    r = client.post("/api/avatar/stream", json=BODY)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.content == b"".join(CHUNKS)
    clips = list(d.glob("*.mp4"))
    assert len(clips) == 1 and clips[0].read_bytes() == r.content
    assert not list(d.glob("*.tmp"))
    # second call: cached, Boson untouched, JSON pointer to the file
    n = len(calls)
    r2 = client.post("/api/avatar/stream", json=BODY)
    assert r2.json() == {"video_url": f"/media/avatar/{clips[0].name}"} and len(calls) == n
    # and the existing async endpoint also sees the cached clip
    assert client.post("/api/avatar/talk", json=BODY).json() == r2.json()


def test_stream_failure_removes_temp_and_caches_nothing(tmp_path, monkeypatch):
    client, _, d = stream_env(tmp_path, monkeypatch, fail_after=2)
    with pytest.raises(Exception):
        client.post("/api/avatar/stream", json=BODY)
    assert not list(d.glob("*.mp4")) and not list(d.glob("*.tmp"))
    assert main._avatar_locks == {}


def test_stream_oversize_is_cut_off(tmp_path, monkeypatch):
    client, _, d = stream_env(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "STREAM_MAX_BYTES", 10)
    with pytest.raises(Exception):
        client.post("/api/avatar/stream", json=BODY)
    assert not list(d.glob("*.mp4")) and not list(d.glob("*.tmp"))


def test_stream_boson_busy_is_a_clean_429_and_releases_lock(tmp_path, monkeypatch):
    client, _, d = stream_env(tmp_path, monkeypatch, status=429, headers={"Retry-After": "0"})
    r = client.post("/api/avatar/stream", json=BODY)
    assert r.status_code == 429
    assert not list(d.glob("*")) and main._avatar_locks == {}


def test_stream_validation_gating_and_rate_limit(tmp_path, monkeypatch):
    client, calls, _ = stream_env(tmp_path, monkeypatch)
    for body in ({"audio_url": "https://evil/x.mp3", "voice": "voice_abc"},
                 {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "marcus"}):
        assert client.post("/api/avatar/stream", json=body).status_code == 400
    assert client.post("/api/avatar/stream", json={**BODY, "audio_url": f"/media/tts/{'b' * 64}.mp3"}).status_code == 404
    assert calls == []
    monkeypatch.setattr(settings, "access_token", "letmein")
    assert client.post("/api/avatar/stream", json=BODY).status_code == 401
    assert client.post("/api/avatar/stream", json=BODY, headers={"X-Talkback-Token": "letmein"}).status_code == 200
    monkeypatch.setattr(main, "avatar_bucket", TokenBucket(0, 60))
    (tmp_path / "cache" / "avatar" / "talk").joinpath(*[]).mkdir(exist_ok=True)
    for f in (tmp_path / "cache" / "avatar" / "talk").glob("*.mp4"):
        f.unlink()
    assert client.post("/api/avatar/stream", json=BODY, headers={"X-Talkback-Token": "letmein"}).status_code == 429
