"""PER_USER hardening: startup check, 401 on bad IAP header, key rotation, off-loop verification, gated lip-sync
clips, avatar folder collision, anonymous writes, legacy face<->voice binding in the migration."""
import asyncio
import hashlib
import json
import time

import pytest
from google.auth import crypt, jwt

from app import main, users
from app.config import settings
from tests.test_api import env  # noqa: F401  (fixture)
from tests.test_per_user import (ALICE, AUD, BOB, OWNER, PNG, _PRIV, _PUB, _Resp, h, pu, seed_shared,  # noqa: F401
                                 token, upload_face)


def test_startup_refuses_per_user_without_audience():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="IAP_AUDIENCE"):
        settings.__class__(per_user=True, iap_audience="")
    assert settings.__class__(per_user=True, iap_audience=AUD).per_user
    assert settings.__class__().per_user is False


def test_unknown_kid_refetches_certs_once(pu, monkeypatch):
    client, _ = pu
    served = [{"k0": _PUB}, {"k1": _PUB}]
    fetched = []

    def fake(url, method="GET", **kw):
        fetched.append(url)
        return _Resp(json.dumps(served[min(len(fetched) - 1, 1)]).encode())
    tr = users._CertsRequest(fake)
    monkeypatch.setattr(users, "_transport", lambda: tr)
    assert client.get("/api/me", headers=h(token(ALICE))).json()["email"] == ALICE
    assert len(fetched) == 2
    bad = jwt.encode(crypt.ES256Signer.from_string(_PRIV, "k9"),
                     {"iss": users.IAP_ISSUER, "aud": AUD, "email": BOB, "exp": int(time.time()) + 600}).decode()
    assert client.get("/api/me", headers=h(bad)).status_code == 401
    assert len(fetched) == 2   # a second unknown key id right away does not refetch (rate limited) and fails closed


def test_cert_fetch_failure_is_negatively_cached(pu, monkeypatch):
    client, _ = pu
    fetched = []

    def fake(url, method="GET", **kw):
        fetched.append(url)
        raise OSError("network down")
    tr = users._CertsRequest(fake)
    monkeypatch.setattr(users, "_transport", lambda: tr)
    for who in (ALICE, BOB, OWNER):
        assert client.get("/api/me", headers=h(token(who))).status_code == 401
    assert len(fetched) == 1


def test_verification_runs_off_the_event_loop(pu, monkeypatch):
    client, _ = pu
    seen = []
    real = users.verify_email

    def spy(tok):
        try:
            asyncio.get_running_loop()
            seen.append("loop")
        except RuntimeError:
            seen.append("thread")
        return real(tok)
    monkeypatch.setattr(users, "verify_email", spy)
    assert client.get("/api/me", headers=h(token(ALICE))).json()["email"] == ALICE
    assert seen == ["thread"]


def lip_setup(who):
    """A voice + face for `who`, a cached TTS file and a rendered lip-sync clip in their own folder."""
    ident = users.Identity(who, users.ns_for(who))
    root = users.user_root(ident)
    root.mkdir(parents=True, exist_ok=True)
    (root / "voices.json").write_text(json.dumps({"Me": "voice_me"}))
    (root / "faces").mkdir()
    (root / "faces" / "f1.png").write_bytes(PNG)
    (root / "faces.json").write_text(json.dumps({"f1": {"name": "Me", "file": "f1.png", "voice": "voice_me"}}))
    tts = settings.data_dir / "cache" / "tts"
    tts.mkdir(parents=True, exist_ok=True)
    k = "a" * 64
    (tts / f"{k}.mp3").write_bytes(b"mp3")
    key = f"{k}-{hashlib.sha256(PNG).hexdigest()[:16]}"
    (root / "lipsync").mkdir()
    (root / "lipsync" / f"{key}.mp4").write_bytes(b"MP4")
    return {"audio_url": f"/media/tts/{k}.mp3", "voice": "voice_me"}, key


@pytest.mark.parametrize("route", ["/api/avatar/stream", "/api/avatar/talk"])
def test_lipsync_clips_are_per_user_and_gated(pu, route):
    client, _ = pu
    body, key = lip_setup(ALICE)
    r = client.post(route, headers=h(token(ALICE)), json=body)
    assert r.status_code == 200 and r.json()["video_url"] == f"/media/avatar/{key}.mp4"
    url = r.json()["video_url"]
    assert client.get(url, headers=h(token(ALICE))).content == b"MP4"
    assert client.get(url, headers=h(token(BOB))).status_code == 404
    assert client.get(url).status_code == 404
    assert client.post(route, headers=h(token(BOB)), json=body).status_code == 400   # not Bob's voice
    assert client.post(route, json=body).status_code == 400


def test_legacy_shared_lipsync_folder_is_not_served(pu):
    client, _ = pu
    d = settings.data_dir / "cache" / "avatar" / "talk"
    d.mkdir(parents=True)
    (d / "old.mp4").write_bytes(b"MP4")
    for hd in ({}, h(token(OWNER)), h(token(ALICE))):
        assert client.get("/media/avatar/old.mp4", headers=hd).status_code == 404


def test_book_avatar_routes_cannot_reach_the_lipsync_folder(pu):
    client, _ = pu
    d = settings.data_dir / "cache" / "avatar" / "talk"
    d.mkdir(parents=True)
    (d / "old.mp4").write_bytes(b"MP4")
    for hd in ({}, h(token(OWNER))):
        assert client.get("/api/avatars/talk", headers=hd).status_code == 404
        assert client.get("/api/avatar/old?book=talk", headers=hd).status_code == 404
    bk = settings.data_dir / "cache" / "avatar" / "demo"
    bk.mkdir()
    (bk / "p0c0.mp4").write_bytes(b"MP4")
    assert client.get("/api/avatars/demo").json() == ["p0c0"]
    assert client.get("/api/avatar/p0c0?book=demo").status_code == 200


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_anonymous_writes_stay_in_public_and_never_touch_accounts(pu):
    client, _ = pu
    root = settings.data_dir
    upload_face(client, ALICE)
    client.put("/api/books/demo/cast", headers=h(token(ALICE)), json={"main": ["king"]})
    before = {k: v for k, v in snapshot(root).items() if k.startswith("users/") or k.endswith("owner.txt")}
    client.post("/api/faces", data={"name": "X"}, files={"image": ("a.png", PNG, "image/png")})
    client.put("/api/books/demo/cast", json={"main": ["harry"]})
    client.post("/api/books/upload", data={"title": "Anon", "kind": "textbook"},
                files={"file": ("a.txt", b"Hello there.\n\nSecond paragraph here.", "text/plain")})
    for bid in ("u-alice-1", "u-bob-1"):
        assert client.post(f"/api/books/{bid}/kind", json={"kind": "novel"}).status_code == 404
        assert client.post(f"/api/books/{bid}/enrich").status_code == 404
        assert client.put(f"/api/books/{bid}/cast", json={"main": []}).status_code == 404
    fid = client.get("/api/faces", headers=h(token(ALICE))).json()[0]["id"]
    assert client.delete(f"/api/faces/{fid}").status_code == 403   # anonymous may not delete at all
    assert client.post(f"/api/faces/{fid}/animate").status_code == 404
    after = snapshot(root)
    assert {k: v for k, v in after.items() if k in before} == before   # nothing existing was altered
    new = {k for k in after if k.startswith("users/") and k not in before}
    assert new and all(k.startswith("users/public/") for k in new)
    assert {p.name for p in (root / "users").iterdir()} == {users.ns_for(ALICE), "public"}
    owners = {p.parent.name: p.read_text() for p in (root / "books").glob("*/owner.txt")}
    assert owners["u-alice-1"] == users.ns_for(ALICE) and owners["u-bob-1"] == users.ns_for(BOB)
    assert sum(v == "public" for v in owners.values()) == 1   # the anonymous upload


def test_migration_keeps_legacy_voice_face_binding(pu):
    client, _ = pu
    root = settings.data_dir
    (root / "voices.json").write_text(json.dumps({"Cloned narrator": "voice_x"}))
    (root / "portraits" / "voices").mkdir(parents=True)
    (root / "portraits" / "voices" / "x.png").write_bytes(PNG)
    (root / "portraits" / "voices" / "x-talk.mp4").write_bytes(b"mp4")
    (root / "voice_faces.json").write_text(json.dumps({"voice_x": "x.png"}))   # never folded into faces.json
    users.migrate_on_startup()
    ow = h(token(OWNER))
    faces_list = client.get("/api/faces", headers=ow).json()
    assert [(f["name"], f["voice"]) for f in faces_list] == [("Cloned narrator", "voice_x")]
    assert faces_list[0]["talk"] is not None
    assert client.get("/api/voices/faces", headers=ow).json().keys() == {"voice_x"}
    assert client.get(faces_list[0]["image"], headers=ow).content == PNG
    assert (root / "voice_faces.json").is_file()   # original kept
    assert users.migrate_to_owner(root, OWNER) == []   # idempotent


def test_migration_does_not_duplicate_an_already_folded_binding(pu):
    root = settings.data_dir
    seed_shared(root)   # faces.json already binds voice_x
    (root / "voice_faces.json.migrated").write_text("{}")
    (root / "voice_faces.json").write_text(json.dumps({"voice_x": "x.png"}))
    (root / "portraits" / "voices").mkdir(parents=True)
    (root / "portraits" / "voices" / "x.png").write_bytes(PNG)
    users.migrate_on_startup()
    ent = json.loads((root / "users" / users.ns_for(OWNER) / "faces.json").read_text())
    assert [e["voice"] for e in ent.values()] == ["voice_x"]


def test_migration_copies_lipsync_clips_to_the_owner(pu):
    root = settings.data_dir
    d = root / "cache" / "avatar" / "talk"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"MP4")
    users.migrate_on_startup()
    assert (root / "users" / users.ns_for(OWNER) / "lipsync" / "c.mp4").read_bytes() == b"MP4"
    assert (d / "c.mp4").is_file()
