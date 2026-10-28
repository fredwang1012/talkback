"""Live avatar sessions (#140): LiveKit tokens, Boson session body, one session per account, fallback signals."""
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app import live_avatar, main
from app.boson import BosonClient
from app.config import settings
from tests.test_api import env  # noqa: F401  (env is a fixture)


def _claims(token: str) -> dict:
    head, body, sig = token.split(".")
    want = base64.urlsafe_b64encode(hmac.new(b"secret-xyz", f"{head}.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=")
    assert sig.encode() == want  # signed with LIVEKIT_API_SECRET
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


@pytest.fixture
def lk(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://demo.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APIkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "secret-xyz")
    monkeypatch.setattr(settings, "boson_api_key", "k")
    monkeypatch.setattr(settings, "live_avatar_max_sessions", 2)
    monkeypatch.setattr(main, "live_avatar_bucket", main.TokenBucket(100, 60))
    monkeypatch.setattr(live_avatar, "_sessions", {})
    state = {"created": [], "deleted": [], "create_status": 201, "n": 0}

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path == "/v1/avatars":
            return httpx.Response(200, json={"object": "list", "data": [
                {"avatar_id": "builtin_claire_v1", "name": "Claire", "is_public": True},
                {"avatar_id": "builtin_james_v1", "name": "James", "is_public": True}]})
        if req.method == "POST" and req.url.path == "/v1/avatar/sessions":
            state["created"].append(json.loads(req.content))
            if state["create_status"] != 201:
                return httpx.Response(state["create_status"], headers={"Retry-After": "5"},
                                      json={"error": {"type": "rate_limit_error", "code": "capacity_exhausted", "message": "busy"}})
            state["n"] += 1
            return httpx.Response(201, json={"id": f"avrt_{state['n']}", "object": "avatar.realtime.session", "status": "active",
                                             "avatar_identity": "boson-avatar", "expires_at": "2026-10-28T01:00:00Z"})
        if req.method == "DELETE" and req.url.path.startswith("/v1/avatar/sessions/"):
            state["deleted"].append(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"status": "releasing"})
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "unexpected"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return state


def test_off_without_livekit_settings(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "livekit_url", "")
    assert client.get("/api/live-avatar").json() == {"enabled": False, "avatars": []}
    assert client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_claire_v1"}).status_code == 404


def test_lists_presets(env, lk):  # noqa: F811
    client, _ = env
    assert client.get("/api/live-avatar").json() == {"enabled": True, "avatars": [
        {"id": "builtin_claire_v1", "name": "Claire"}, {"id": "builtin_james_v1", "name": "James"}]}


def test_start_mints_tokens_and_asks_boson_for_the_room(env, lk):  # noqa: F811
    client, _ = env
    r = client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_claire_v1"})
    assert r.status_code == 200
    out = r.json()
    body = lk["created"][0]
    tr = body["transport"]
    assert body["avatar_id"] == "builtin_claire_v1" and body["max_duration_seconds"] == settings.live_avatar_max_seconds
    assert tr["url"] == "wss://demo.livekit.cloud" and tr["audio_source"] == "data_stream"
    assert tr["publisher_identity"] == out["identity"] and tr["participant_identity"] == "boson-avatar"
    avatar = _claims(tr["participant_token"])
    reader = _claims(out["token"])
    assert avatar["iss"] == reader["iss"] == "APIkey"
    assert avatar["sub"] == "boson-avatar" and avatar["video"]["room"] == tr["room_name"] == reader["video"]["room"]
    assert avatar["video"]["canPublishSources"] == ["camera", "microphone"] and avatar["video"]["canSubscribe"] is False
    assert avatar["exp"] - avatar["nbf"] > settings.live_avatar_max_seconds  # outlives the session
    assert reader["sub"] == out["identity"] and reader["video"]["canSubscribe"] and reader["video"]["canPublishData"]
    assert "secret-xyz" not in json.dumps(out)


def test_one_session_per_account_and_release(env, lk):  # noqa: F811
    client, _ = env
    a = client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_claire_v1"}).json()
    b = client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_james_v1"}).json()
    assert lk["deleted"] == [a["session"]]  # the older one is released first
    assert client.delete(f"/api/live-avatar/sessions/{b['session']}").status_code == 200
    assert lk["deleted"] == [a["session"], b["session"]]
    assert client.delete(f"/api/live-avatar/sessions/{b['session']}").status_code == 404


def test_busy_boson_or_full_server_says_use_clips(env, lk, monkeypatch):  # noqa: F811
    client, _ = env
    lk["create_status"] = 429
    r = client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_claire_v1"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "live_unavailable"
    lk["create_status"] = 201
    monkeypatch.setattr(settings, "live_avatar_max_sessions", 0)
    r = client.post("/api/live-avatar/sessions", json={"avatar_id": "builtin_claire_v1"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "live_unavailable"


def test_parallel_starts_cannot_pass_the_cap(env, lk, monkeypatch):  # noqa: F811
    """Two accounts starting at once: the lock makes check + start + record one step (security review)."""
    import asyncio

    monkeypatch.setattr(settings, "live_avatar_max_sessions", 1)

    async def both():
        reqs = [main.LiveAvatarStart(avatar_id="builtin_claire_v1") for _ in range(2)]
        owners = iter(["acct-a", "acct-b"])
        monkeypatch.setattr(main, "_live_owner", lambda request: next(owners))
        monkeypatch.setattr(main, "rate_key", lambda request: "ip")
        return await asyncio.gather(*(main.start_live_avatar(r, None) for r in reqs), return_exceptions=True)

    out = asyncio.run(both())
    assert sum(isinstance(o, dict) for o in out) == 1
    assert len(lk["created"]) == 1


def test_one_account_starting_twice_at_once_gets_one_session(env, lk, monkeypatch):  # noqa: F811
    import asyncio

    monkeypatch.setattr(main, "_live_owner", lambda request: "acct-a")
    monkeypatch.setattr(main, "rate_key", lambda request: "ip")

    async def both():
        reqs = [main.LiveAvatarStart(avatar_id="builtin_claire_v1") for _ in range(2)]
        return await asyncio.gather(*(main.start_live_avatar(r, None) for r in reqs), return_exceptions=True)

    asyncio.run(both())
    from app import live_avatar
    assert len(live_avatar.held_by("acct-a")) == 1             # never two held at once for one account
    assert len(lk["created"]) - len(lk["deleted"]) == 1       # any extra one was released at Boson
