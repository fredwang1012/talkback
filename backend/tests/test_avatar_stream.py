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


# ---- review fixes: lock lifetime, concurrent renders, stalled upstream ----

def test_cached_after_wait_releases_the_lock_and_next_request_proceeds(tmp_path, monkeypatch):
    import asyncio
    client, calls, d = stream_env(tmp_path, monkeypatch)
    key = main._lipsync_inputs(main.LipSyncRequest(**BODY))[2]
    d.mkdir(parents=True, exist_ok=True)

    async def scenario():
        await main._acquire_avatar_key(key)  # a render is "in progress"
        req = main.LipSyncRequest(**BODY)
        task = asyncio.create_task(main.lipsync_stream(req, _FakeRequest()))
        await asyncio.sleep(0.01)
        (d / f"{key}.mp4").write_bytes(b"done")  # the other render finishes and caches
        main._release_avatar_key(key)
        out = await asyncio.wait_for(task, 2)
        assert out == {"video_url": f"/media/avatar/{key}.mp4"}
        # the lock must be free again: a fresh acquire does not hang
        await asyncio.wait_for(main._acquire_avatar_key(key), 1)
        main._release_avatar_key(key)

    asyncio.run(scenario())
    assert main._avatar_locks == {}


class _FakeRequest:
    client = type("C", (), {"host": "1.2.3.4"})()
    headers: dict = {}


def test_three_concurrent_requests_cause_one_boson_render(tmp_path, monkeypatch):
    import asyncio
    _, calls, _ = stream_env(tmp_path, monkeypatch)

    # drive three requests through the real app concurrently with an async client
    async def hammer():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            rs = await asyncio.gather(*[c.post("/api/avatar/stream", json=BODY) for _ in range(3)])
        return rs

    rs = asyncio.run(hammer())
    assert [r.status_code for r in rs] == [200, 200, 200]
    assert calls.count("/v1/videos/stream") == 1
    assert main._avatar_locks == {}


def test_stalled_upstream_is_cut_off_by_the_overall_deadline(tmp_path, monkeypatch):
    import asyncio
    client, _, d = stream_env(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "STREAM_MAX_SECONDS", 0.3)

    async def stalling():
        yield CHUNKS[0]
        await asyncio.sleep(30)
        yield CHUNKS[1]

    def handler(req):
        return httpx.Response(200, content=stalling(), headers={"X-Video-Id": "v"})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    import time
    t = time.monotonic()
    with pytest.raises(Exception):
        client.post("/api/avatar/stream", json=BODY)
    assert time.monotonic() - t < 5
    assert not list(d.glob("*.mp4")) and not list(d.glob("*.tmp")) and main._avatar_locks == {}
