"""Lip-sync output size (A7): configurable via AVATAR_SIZE; the size reaches Boson, the clip cache key and the usage price."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app import main
from app.boson import BosonClient
from app.config import Settings, settings
from tests.test_faces import KEY, lipsync_env

BODY = {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "voice_abc"}


def test_default_size_is_square_and_only_known_sizes_are_accepted():
    assert Settings(_env_file=None).avatar_size == "640x640"
    assert Settings(_env_file=None, avatar_size="480x640").avatar_size == "480x640"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, avatar_size="1000x1000")


async def test_create_video_defaults_to_the_configured_size(monkeypatch):
    sizes = []

    def handler(req):
        if req.method == "POST":
            sizes.append(json.loads(req.content)["size"])
            return httpx.Response(200, json={"id": "v"})
        return httpx.Response(404)

    monkeypatch.setattr(settings, "avatar_size", "480x640")
    monkeypatch.setattr(settings, "boson_api_key", "k" * 20)
    c = BosonClient(transport=httpx.MockTransport(handler))
    await c.create_video("aW1n", "YXVkaW8=")
    await c.create_video("aW1n", "YXVkaW8=", size="640x480")
    assert sizes == ["480x640", "640x480"]


@pytest.mark.parametrize("path", ["/api/avatar/talk", "/api/avatar/stream"])
def test_request_carries_the_size_and_the_cache_key_separates_sizes(tmp_path, monkeypatch, path):
    monkeypatch.setattr(settings, "boson_api_key", "k" * 20)
    client, _calls = lipsync_env(tmp_path, monkeypatch)
    sent = []

    def handler(req: httpx.Request):
        if req.method == "POST" and req.url.path.startswith("/v1/videos"):
            sent.append(json.loads(req.content)["size"])
            if req.url.path.endswith("/stream"):
                return httpx.Response(200, content=b"ftypiso5moof", headers={"X-Video-Id": "video_1"})
            return httpx.Response(200, json={"id": "video_1"})
        if req.url.path == "/v1/videos/video_1":
            return httpx.Response(200, json={"id": "video_1", "status": "completed"})
        if req.url.path == "/v1/videos/video_1/content":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "no"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    clips = lambda: sorted(p.name for p in (tmp_path / "cache" / "avatar" / "talk").glob("*.mp4"))  # noqa: E731

    assert client.post(path, json=BODY).status_code == 200
    square = clips()
    assert sent == ["640x640"] and len(square) == 1 and "480x640" not in square[0]
    monkeypatch.setattr(settings, "avatar_size", "480x640")
    assert client.post(path, json=BODY).status_code == 200  # same audio and face: a new render, not the square clip
    assert sent == ["640x640", "480x640"]
    assert len(clips()) == 2 and sum("480x640" in c for c in clips()) == 1
    assert client.post(path, json=BODY).status_code == 200 and len(sent) == 2  # and the portrait clip is cached in turn


def _usage_script():
    spec = importlib.util.spec_from_file_location("usage_script", Path(__file__).resolve().parents[2] / "scripts" / "usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_usage_records_the_size_and_the_script_prices_it(tmp_path, monkeypatch):
    from app.gcp import usage
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(usage, "_offload", False)
    monkeypatch.setattr(settings, "boson_api_key", "k" * 20)
    import asyncio

    async def run():
        c = BosonClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "v"})))
        await c.create_video("aW1n", "YXVkaW8=", size="480x640", seconds=60)
    asyncio.run(run())
    row = json.loads((tmp_path / "cache" / "usage.jsonl").read_text().splitlines()[-1])
    assert row["size"] == "480x640"
    u = _usage_script()
    assert u.cost_of(row) == pytest.approx(0.07)
