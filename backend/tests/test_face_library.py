"""Face library (/api/faces): upload validation, list/delete, migration, animation (Boson mocked), gating."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import faces, main
from app.boson import BosonClient
from app.config import settings
from app.ratelimit import TokenBucket

PNG = b"\x89PNG\r\n\x1a\nfake"
JPG = b"\xff\xd8\xff\xe0fake"
WEBP = b"RIFF\x00\x00\x00\x00WEBPfake"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(main, "avatar_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "face_bucket", TokenBucket(1000, 60))
    main._face_locks.clear()
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path == "/v1/audio/speech":
            return httpx.Response(200, json={"audio": "TVAz", "response_format": "mp3", "input": "x"})
        if req.method == "POST" and req.url.path == "/v1/videos":
            return httpx.Response(200, json={"id": "video_1"})
        if req.url.path == "/v1/videos/video_1":
            return httpx.Response(200, json={"id": "video_1", "status": "completed"})
        if req.url.path == "/v1/videos/video_1/content":
            return httpx.Response(200, content=b"MP4DATA")
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "no"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(settings, "boson_api_key", "k" * 20)

    real_sleep = asyncio.sleep

    async def no_sleep(_):
        await real_sleep(0)  # no waiting, but still yields so cancellation and sibling tasks run
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return TestClient(main.app), calls


def upload(client, data=PNG, name="Ana", filename="a.png"):
    return client.post("/api/faces", data={"name": name}, files={"image": (filename, data, "image/png")})


def test_upload_list_delete(lib):
    client, _ = lib
    r = upload(client)
    assert r.status_code == 200
    face = r.json()
    assert face["name"] == "Ana" and face["talk"] is None and face["idle"] is None
    assert face["image"] == f"/portraits/faces/{face['id']}.png"
    assert (settings.data_dir / "portraits" / "faces" / f"{face['id']}.png").read_bytes() == PNG
    assert client.get("/api/faces").json() == [face]
    assert client.delete(f"/api/faces/{face['id']}").status_code == 200
    assert client.get("/api/faces").json() == []
    assert not list((settings.data_dir / "portraits" / "faces").iterdir())
    assert client.delete(f"/api/faces/{face['id']}").status_code == 404


def test_jpeg_and_webp_accepted_and_type_comes_from_the_bytes(lib):
    client, _ = lib
    assert upload(client, JPG, filename="x.png").json()["image"].endswith(".jpg")
    assert upload(client, WEBP, filename="x.bin").json()["image"].endswith(".webp")


def test_upload_rejects_bad_input(lib):
    client, _ = lib
    assert upload(client, b"MZ\x90\x00", filename="x.exe").status_code == 400
    assert upload(client, b"not a png at all", filename="x.png").status_code == 400  # fake PNG: extension lies
    assert upload(client, PNG + b"\0" * (5 * 1024 * 1024 + 1)).status_code == 400
    assert upload(client, name="  ").status_code == 400
    assert client.get("/api/faces").json() == []


def test_path_ids_are_validated(lib):
    client, calls = lib
    assert client.delete("/api/faces/bad.id").status_code == 404
    assert client.post("/api/faces/bad.id/animate").status_code == 404
    assert client.post("/api/faces/fnope/animate").status_code == 404
    assert calls == []


def test_animate_renders_once_and_is_idempotent(lib):
    client, calls = lib
    fid = upload(client).json()["id"]
    r = client.post(f"/api/faces/{fid}/animate")
    assert r.status_code == 200
    body = r.json()
    assert body["animated"] is True and body["approx_cost_usd"] > 0
    assert body["talk"].endswith("-talk.mp4") and body["idle"].endswith("-idle.mp4")
    assert (settings.data_dir / "portraits" / "faces" / f"{fid}-talk.mp4").read_bytes() == b"MP4DATA"
    n = len(calls)
    again = client.post(f"/api/faces/{fid}/animate").json()
    assert again["animated"] is False and again["approx_cost_usd"] == 0 and again["talk"] == body["talk"]
    assert len(calls) == n, "loops exist: Boson is not called again"
    assert client.get("/api/faces").json()[0]["talk"] == body["talk"]


def test_animate_only_renders_the_missing_loop(lib):
    client, calls = lib
    fid = upload(client).json()["id"]
    faces.loop_path(fid, "talk").write_bytes(b"old")
    client.post(f"/api/faces/{fid}/animate")
    assert [c for c in calls if c[1] == "/v1/audio/speech"] == [], "talk loop exists: no TTS line"
    assert faces.loop_path(fid, "talk").read_bytes() == b"old"
    assert faces.loop_path(fid, "idle").read_bytes() == b"MP4DATA"


def test_double_click_does_not_double_spend(lib):
    _, calls = lib
    fid = upload(lib[0]).json()["id"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            return await asyncio.gather(c.post(f"/api/faces/{fid}/animate"), c.post(f"/api/faces/{fid}/animate"))

    a, b = asyncio.run(go())
    assert a.status_code == b.status_code == 200
    assert sorted([a.json()["animated"], b.json()["animated"]]) == [False, True]
    assert len([c for c in calls if c == ("POST", "/v1/videos")]) == 2  # talk + idle once, not four


def test_animate_is_rate_limited_but_idempotent_hits_are_free(lib, monkeypatch):
    client, calls = lib
    monkeypatch.setattr(main, "avatar_bucket", TokenBucket(1, 3600))
    a, b = upload(client).json()["id"], upload(client, JPG).json()["id"]
    assert client.post(f"/api/faces/{a}/animate").status_code == 200
    assert client.post(f"/api/faces/{b}/animate").status_code == 429
    assert client.post(f"/api/faces/{a}/animate").status_code == 200  # nothing to render: no token needed
    assert faces.get_face(b)["talk"] is None


def test_animate_failure_is_a_clean_error(lib, monkeypatch):
    client, _ = lib
    fid = upload(client).json()["id"]
    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"error": {"type": "insufficient_quota", "message": "no credit"}}))))
    r = client.post(f"/api/faces/{fid}/animate")
    assert r.status_code == 402
    assert faces.get_face(fid)["talk"] is None


def test_legacy_voice_faces_migrate_into_the_library(lib):
    client, _ = lib
    d = settings.data_dir
    (d / "portraits" / "voices").mkdir(parents=True)
    (d / "portraits" / "voices" / "voice_abc.png").write_bytes(PNG)
    (d / "portraits" / "voices" / "voice_abc-talk.mp4").write_bytes(b"mp4")
    (d / "voice_faces.json").write_text(json.dumps({"voice_abc": "voice_abc.png"}), encoding="utf-8")
    (d / "voices.json").write_text(json.dumps({"narrator": "voice_abc"}), encoding="utf-8")
    listed = client.get("/api/faces").json()
    assert len(listed) == 1 and listed[0]["name"] == "narrator" and listed[0]["voice"] == "voice_abc"
    assert listed[0]["talk"].endswith("-talk.mp4") and listed[0]["idle"] is None
    assert client.get("/api/voices/faces").json() == {
        "voice_abc": {"image": listed[0]["image"], "talk": listed[0]["talk"], "idle": None}}
    assert len(client.get("/api/faces").json()) == 1, "migration runs once"


def test_library_endpoints_need_the_access_code(lib, monkeypatch):
    client, _ = lib
    fid = upload(client).json()["id"]
    monkeypatch.setattr(settings, "access_token", "s3cret")
    assert upload(client).status_code == 401
    assert client.get("/api/faces").status_code == 401
    assert client.delete(f"/api/faces/{fid}").status_code == 401
    assert client.post(f"/api/faces/{fid}/animate").status_code == 401
    h = {"X-Talkback-Token": "s3cret"}
    assert client.get("/api/faces", headers=h).status_code == 200
    assert client.delete(f"/api/faces/{fid}", headers=h).status_code == 200


def test_partial_failure_cancels_the_sibling_render_and_retry_recovers(lib, monkeypatch):
    client, calls = lib
    fid = upload(client).json()["id"]
    st = {"posts": 0, "polls2": 0, "ok": False}

    def handler(req: httpx.Request):
        path = req.url.path
        if path == "/v1/audio/speech":
            return httpx.Response(200, json={"audio": "TVAz", "response_format": "mp3", "input": "x"})
        if req.method == "POST" and path == "/v1/videos":
            st["posts"] += 1
            return httpx.Response(200, json={"id": f"video_{st['posts']}"})
        if path.startswith("/v1/videos/") and path.endswith("/content"):
            return httpx.Response(200, content=b"MP4DATA")
        if path.startswith("/v1/videos/"):
            vid = path.rsplit("/", 1)[1]
            if st["ok"]:
                return httpx.Response(200, json={"id": vid, "status": "completed"})
            if vid == "video_1":  # talk render fails
                return httpx.Response(200, json={"id": vid, "status": "failed", "error": "boom"})
            st["polls2"] += 1  # idle render keeps "processing" until cancelled
            return httpx.Response(200, json={"id": vid, "status": "processing"})
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "no"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    assert client.post(f"/api/faces/{fid}/animate").status_code >= 500
    polls = st["polls2"]
    client.get("/api/faces")  # give the app loop time: a leaked sibling would keep polling
    client.get("/api/faces")
    assert st["polls2"] == polls, "the idle render was cancelled, not left running"
    assert faces.get_face(fid)["talk"] is None and faces.get_face(fid)["idle"] is None
    assert not list(faces.face_dir().glob("*.tmp")) and main._face_locks == {}
    st["ok"] = True
    posts = st["posts"]
    body = client.post(f"/api/faces/{fid}/animate").json()
    assert body["talk"] and body["idle"] and st["posts"] - posts == 2
    assert client.post(f"/api/faces/{fid}/animate").json()["animated"] is False


def test_corrupt_legacy_file_degrades_instead_of_failing(lib):
    client, _ = lib
    (settings.data_dir / "voice_faces.json").write_text("{not json", encoding="utf-8")
    assert client.get("/api/faces").json() == []
    assert client.get("/api/voices/faces").json() == {}
    assert upload(client).status_code == 200


def test_migrated_ids_are_derived_from_the_voice(lib):
    client, _ = lib
    d = settings.data_dir
    (d / "portraits" / "voices").mkdir(parents=True)
    (d / "portraits" / "voices" / "voice_abc.png").write_bytes(PNG)
    legacy = d / "voice_faces.json"
    legacy.write_text(json.dumps({"voice_abc": "voice_abc.png"}), encoding="utf-8")
    first = client.get("/api/faces").json()[0]["id"]
    assert first == client.get("/api/faces").json()[0]["id"]
    assert first.startswith("v") and len(client.get("/api/faces").json()) == 1


def test_delete_during_animate_leaves_no_orphans_and_locks_are_released(lib):
    client, _ = lib
    fid = upload(client).json()["id"]

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            return await asyncio.gather(c.post(f"/api/faces/{fid}/animate"), c.delete(f"/api/faces/{fid}"))

    a, d = asyncio.run(go())
    assert a.status_code in (200, 404) and d.status_code in (200, 404)
    assert not list(faces.face_dir().glob(f"{fid}*")), "no orphan image or loops after delete"
    assert main._face_locks == {}


def test_locks_are_released_after_a_failed_animate(lib, monkeypatch):
    client, _ = lib
    fid = upload(client).json()["id"]
    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"error": {"type": "insufficient_quota", "message": "x"}}))))
    assert client.post(f"/api/faces/{fid}/animate").status_code == 402
    assert main._face_locks == {}
