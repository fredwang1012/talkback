"""#80: a character's answer as one shareable MP4. Pure-Python MP4 work (mp4join) on tiny synthetic clips and MP3s,
and the /api/clip/answer route: only the caller's own cached clips, never a render, bounded, rate limited."""
import base64
import hashlib
import struct

import pytest

from app import deps, mp4join, users
from app.config import settings
from app.mp4join import Mp4Error, _boxes
from app.ratelimit import TokenBucket
from tests.test_api import env  # noqa: F401  (fixture)
from tests.test_faces import KEY, lipsync_env
from tests.test_per_user import ALICE, BOB, h, pu, token, upload_face  # noqa: F401  (pu is a fixture)


# ---- synthetic inputs ------------------------------------------------------------------------------------------

def box(typ, *parts):
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), typ.encode()) + body


def full(typ, ver, flags, *parts):
    return box(typ, struct.pack(">I", (ver << 24) | flags), *parts)


def trak(tid, handler, timescale, entry):
    tkhd = full("tkhd", 0, 3, struct.pack(">IIIII", 0, 0, tid, 0, 0), b"\0" * 52, struct.pack(">II", 64 << 16, 48 << 16))
    mdhd = full("mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, 0, 0x55C4, 0))
    hdlr = full("hdlr", 0, 0, struct.pack(">I4s12x", 0, handler.encode()), b"x\0")
    stbl = box("stbl", full("stsd", 0, 0, struct.pack(">I", 1), entry), full("stts", 0, 0, b"\0" * 4),
               full("stsz", 0, 0, b"\0" * 8))
    return box("trak", tkhd, box("mdia", mdhd, hdlr, box("minf", stbl)))


def fmp4(video, audio, vdur=512, adur=1024, ventry=box("avc1", b"V1"), aentry=box("mp4a", b"A1")):
    """A fragmented MP4 like Boson's: track 1 video (12288/s), track 2 audio (24000/s), one moof+mdat per fragment.
    `video` / `audio`: lists of fragments, each a list of sample payloads. Video sample 0 of a fragment is a sync."""
    init = box("ftyp", b"iso5", b"\0\0\0\1", b"iso5") + box(
        "moov", full("mvhd", 0, 0, b"\0" * 96), trak(1, "vide", 12288, ventry), trak(2, "soun", 24000, aentry),
        box("mvex", full("trex", 0, 0, struct.pack(">IIIII", 1, 1, 0, 0, 0)), full("trex", 0, 0, struct.pack(">IIIII", 2, 1, 0, 0, 0))))
    out = init
    for i, (vf, af) in enumerate(zip(video, audio)):
        def traf(tid, samples, dur, sync_first, data_offset):
            tfhd = full("tfhd", 0, 0x20000 | 0x8, struct.pack(">II", tid, dur))  # default-base-is-moof + duration
            flags = [0 if (sync_first and j == 0) else 0x10000 for j in range(len(samples))]
            trun = full("trun", 0, 0x1 | 0x200 | 0x400, struct.pack(">Ii", len(samples), data_offset),
                        *(struct.pack(">II", len(s), f) for s, f in zip(samples, flags)))
            return box("traf", tfhd, full("tfdt", 1, 0, struct.pack(">Q", 0)), trun)
        # two passes: the moof size fixes the data offsets
        moof = box("moof", full("mfhd", 0, 0, struct.pack(">I", i + 1)), traf(1, vf, vdur, True, 0), traf(2, af, adur, False, 0))
        voff = len(moof) + 8
        aoff = voff + sum(map(len, vf))
        moof = box("moof", full("mfhd", 0, 0, struct.pack(">I", i + 1)), traf(1, vf, vdur, True, voff), traf(2, af, adur, False, aoff))
        out += moof + box("mdat", *vf, *af)
    return out


def mp3(n_frames, rate_idx=1, fill=b"\0"):
    """MPEG-2 Layer III, 64 kbit/s, mono: 24000 Hz (rate_idx 1) or 22050 Hz (0); 576 samples per frame."""
    rate = [22050, 24000][rate_idx]
    size = 72 * 64000 // rate
    return b"".join(bytes([0xFF, 0xF3, 0x80 | (rate_idx << 2), 0xC0]) + fill * (size - 4) for _ in range(n_frames))


def tree(data, start=0, end=None, path=""):
    """{'moov/trak/...': [(content start, end), ...]} of an MP4, for checks."""
    out = {}
    for typ, _, cs, ce in _boxes(data, start, len(data) if end is None else end):
        key = f"{path}/{typ}".lstrip("/")
        out.setdefault(key, []).append((cs, ce))
        if typ in ("moov", "trak", "mdia", "minf", "stbl"):
            for k, v in tree(data, cs, ce, key).items():
                out.setdefault(k, []).extend(v)
    return out


def table(data, t, name, trak_i):
    cs, ce = t[f"moov/trak/mdia/minf/stbl/{name}"][trak_i]
    return data[cs:ce]


def stts_total(data, t, i):
    b = table(data, t, "stts", i)
    n = struct.unpack_from(">I", b, 4)[0]
    return [struct.unpack_from(">II", b, 8 + 8 * k) for k in range(n)]


# ---- join ------------------------------------------------------------------------------------------------------

def test_join_two_clips_copies_samples_in_order_and_keeps_voice_and_face_in_step():
    a = fmp4([[b"V0" * 5, b"v1", b"v2"]], [[b"A0", b"a1"]])  # video 3 x 512/12288 s = 0.125 s, audio 2 x 1024/24000 s
    b = fmp4([[b"W0", b"w1"], [b"W2"]], [[b"B0"], [b"b1", b"b2"]])
    out = mp4join.join_clips([a, b])
    t = tree(out)
    assert out[4:8] == b"ftyp" and "mdat" in t and "moov/trak" in t and len(t["moov/trak"]) == 2
    cs, ce = t["mdat"][0]
    # interleaved per clip: its video then its audio, the bytes unchanged
    assert out[cs:ce] == b"V0" * 5 + b"v1v2" + b"A0a1" + b"W0w1W2" + b"B0b1b2"
    sizes = table(out, t, "stsz", 0)
    assert struct.unpack_from(">5I", sizes, 12) == (10, 2, 2, 2, 2)
    # each sentence's last frame ends with its voice: 2048/24000 s = 1048.6/12288 -> 1049 ticks after clip one
    v = stts_total(out, t, 0)
    assert v == [(2, 512), (1, 25), (2, 512), (1, 548)]  # clip two: 5120/24000 s in all = 2621 ticks
    assert sum(n * d for n, d in v) == round(5 * 1024 * 12288 / 24000)
    assert stts_total(out, t, 1) == [(5, 1024)]
    stss = table(out, t, "stss", 0)
    assert struct.unpack_from(">I", stss, 4)[0] == 3 and struct.unpack_from(">3I", stss, 8) == (1, 4, 6)
    # the chunk offsets point at the samples
    stco = table(out, t, "stco", 0)
    first = struct.unpack_from(">I", stco, 8)[0]
    assert out[first:first + 10] == b"V0" * 5
    # the sample descriptions are the clips' own
    assert box("avc1", b"V1") in table(out, t, "stsd", 0) and box("mp4a", b"A1") in table(out, t, "stsd", 1)


def test_join_refuses_clips_in_different_formats_or_broken_input():
    a = fmp4([[b"v"]], [[b"a"]])
    other = fmp4([[b"v"]], [[b"a"]], ventry=box("avc1", b"V2"))
    with pytest.raises(Mp4Error):
        mp4join.join_clips([a, other])
    for bad in (b"", b"garbage" * 10, a[:40], box("ftyp", b"isom") + box("mdat", b"x")):
        with pytest.raises(Mp4Error):
            mp4join.join_clips([bad])
    with pytest.raises(Mp4Error):
        mp4join.join_clips([])
    # a sample that points outside the file
    with pytest.raises(Mp4Error):
        mp4join.join_clips([a[:-1]])


def test_parse_reads_trun_sample_layout():
    p = mp4join.parse_fragmented(fmp4([[b"x1", b"yy2"]], [[b"zzz3"]]))
    assert p["vide"].data == [b"x1", b"yy2"] and p["vide"].syncs == [True, False] and p["vide"].timescale == 12288
    assert p["soun"].data == [b"zzz3"] and p["soun"].durations == [1024]


# ---- still ------------------------------------------------------------------------------------------------------

def i420(w=32, hgt=32, y=0):
    return bytes([y]) * (w * hgt) + bytes([128]) * (w * hgt // 2)


def test_still_is_h264_pcm_with_the_mp3_audio():
    audio = [mp3(30), mp3(55, fill=b"\x01")]  # 85 frames x 576 / 24000 = 2.04 s
    out = mp4join.still_mp4(i420(), 32, 32, audio)
    t = tree(out)
    v = stts_total(out, t, 0)
    assert v == [(2, 1000), (1, 40)]  # one frame a second, the last one ends with the voice
    assert stts_total(out, t, 1) == [(85, 576)]
    stsd_v, stsd_a = table(out, t, "stsd", 0), table(out, t, "stsd", 1)
    assert b"avc1" in stsd_v and b"avcC" in stsd_v
    assert b"mp4a" in stsd_a and bytes([0x6B, 0x15]) in stsd_a  # MP3 in MP4
    cs, ce = t["mdat"][0]
    assert mp3(30)[:192] in out[cs:ce] and out[cs:ce].count(mp3(1)) == 30  # the MP3 frames are copied as they are
    # sample 1: an IDR made of I_PCM macroblocks: 4 of them, 384 picture bytes each (the 0s raised to 1)
    first = struct.unpack_from(">I", table(out, t, "stco", 0), 8)[0]
    n = struct.unpack_from(">I", out, first)[0]
    nal = out[first + 4:first + 4 + n]
    assert nal[0] & 0x1F == 5 and nal.count(b"\x01" * 256) >= 4
    # 1 sync sample (frames 2 and 3 are skipped P frames of a few bytes)
    stss = table(out, t, "stss", 0)
    assert struct.unpack_from(">II", stss, 4) == (1, 1)
    sizes = struct.unpack_from(">3I", table(out, t, "stsz", 0), 12)
    assert sizes[1] < 16 and sizes[2] < 16


def test_still_rejects_bad_pictures_and_mixed_audio():
    with pytest.raises(Mp4Error):
        mp4join.still_mp4(i420()[:-1], 32, 32, [mp3(3)])
    with pytest.raises(Mp4Error):
        mp4join.still_mp4(i420(30, 30), 30, 30, [mp3(3)])
    with pytest.raises(Mp4Error):
        mp4join.still_mp4(i420(), 32, 32, [mp3(3), mp3(3, rate_idx=0)])
    with pytest.raises(Mp4Error):
        mp4join.still_mp4(i420(), 32, 32, [b"not audio at all"])


def test_nal_emulation_prevention():
    assert mp4join._nal(3, 5, b"\0\0\0\0\0\1\0\0\4")[1:] == b"\0\0\3\0\0\3\0\1\0\0\4"


# ---- the route -------------------------------------------------------------------------------------------------

def clip_path(face_bytes: bytes, folder):
    key = f"{KEY}-{hashlib.sha256(face_bytes).hexdigest()[:16]}"
    return folder / f"{key if settings.avatar_size == '640x640' else f'{key}-{settings.avatar_size}'}.mp4"


def route_env(tmp_path, monkeypatch):
    client, calls = lipsync_env(tmp_path, monkeypatch)
    monkeypatch.setattr(deps, "clip_bucket", TokenBucket(1000, 60))
    face = next((tmp_path / "portraits" / "faces").glob("*.*"))
    folder = tmp_path / "cache" / "avatar" / "talk"
    folder.mkdir(parents=True, exist_ok=True)
    return client, calls, face.read_bytes(), folder


SENT = {"audio_url": f"/media/tts/{KEY}.mp3", "voice": "voice_abc"}


def test_route_joins_the_cached_clips_and_never_renders(tmp_path, monkeypatch):
    client, calls, face, folder = route_env(tmp_path, monkeypatch)
    body = {"book_id": "alice", "speaker": "The Cat/../x\"", "sentences": [SENT, SENT]}
    r = client.post("/api/clip/answer", json=body)
    assert r.status_code == 409 and "2 of 2" in r.json()["detail"]
    assert calls == []  # a missing clip is never rendered
    clip_path(face, folder).write_bytes(fmp4([[b"v0", b"v1"]], [[b"a0"]]))
    r = client.post("/api/clip/answer", json=body)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"] == 'attachment; filename="talkback-alice-The-Cat-x.mp4"'
    t = tree(r.content)
    cs, ce = t["mdat"][0]
    assert r.content[cs:ce] == b"v0v1a0" * 2
    assert calls == []


def test_route_still_mode_uses_the_cached_audio(tmp_path, monkeypatch):
    client, calls, _, _ = route_env(tmp_path, monkeypatch)
    (tmp_path / "cache" / "tts" / f"{KEY}.mp3").write_bytes(mp3(50))
    from app.routes.clip import STILL_SIDE
    still = base64.b64encode(i420(STILL_SIDE, STILL_SIDE, 90)).decode()
    r = client.post("/api/clip/answer", json={"book_id": "alice", "sentences": [SENT], "still": still})
    assert r.status_code == 200 and r.content[4:8] == b"ftyp" and calls == []
    assert r.headers["content-disposition"].endswith('talkback-alice-answer.mp4"')
    bad = [{"still": base64.b64encode(b"x" * 10).decode()}, {"still": "***"},
           {"sentences": [{**SENT, "audio_url": "/media/tts/../../x.mp3"}], "still": still}]
    codes = [client.post("/api/clip/answer", json={"book_id": "alice", "sentences": [SENT], **b}).status_code for b in bad]
    assert codes == [400, 400, 404]


def test_route_bounds_and_rate_limit(tmp_path, monkeypatch):
    client, _, _, _ = route_env(tmp_path, monkeypatch)
    from app.routes import clip
    too_many = {"book_id": "alice", "sentences": [SENT] * (clip.MAX_SENTENCES + 1)}
    assert client.post("/api/clip/answer", json=too_many).status_code == 422
    assert client.post("/api/clip/answer", json={"book_id": "../x", "sentences": [SENT]}).status_code == 404
    assert client.post("/api/clip/answer", json={"book_id": "alice", "sentences": [{**SENT, "face_id": "../x"}]}).status_code == 404
    monkeypatch.setattr(clip, "MAX_INPUT_BYTES", 10)
    (tmp_path / "cache" / "tts" / f"{KEY}.mp3").write_bytes(mp3(50))
    still = base64.b64encode(i420(clip.STILL_SIDE, clip.STILL_SIDE)).decode()
    assert client.post("/api/clip/answer", json={"book_id": "alice", "sentences": [SENT], "still": still}).status_code == 413
    monkeypatch.setattr(deps, "clip_bucket", TokenBucket(1, 60))
    codes = [client.post("/api/clip/answer", json={"book_id": "alice", "sentences": [SENT]}).status_code for _ in range(2)]
    assert codes[1] == 429


def test_route_uses_only_the_callers_own_clips(pu, monkeypatch):  # noqa: F811
    client, _ = pu
    monkeypatch.setattr(deps, "clip_bucket", TokenBucket(1000, 60))
    fid = upload_face(client, ALICE).json()["id"]
    tts = settings.data_dir / "cache" / "tts"
    tts.mkdir(parents=True, exist_ok=True)
    (tts / f"{KEY}.mp3").write_bytes(mp3(10))
    ns_dir = settings.data_dir / "users" / users.ns_for(ALICE)
    face = next((ns_dir / "faces").glob(f"{fid}.*"))
    (ns_dir / "lipsync").mkdir(parents=True, exist_ok=True)
    clip_path(face.read_bytes(), ns_dir / "lipsync").write_bytes(fmp4([[b"v"]], [[b"a"]]))
    body = {"book_id": "demo", "sentences": [{**SENT, "voice": "marcus", "face_id": fid}]}
    assert client.post("/api/clip/answer", headers=h(token(ALICE)), json=body).status_code == 200
    assert client.post("/api/clip/answer", headers=h(token(BOB)), json=body).status_code == 404   # not his face
    assert client.post("/api/clip/answer", json=body).status_code == 404   # anonymous
    # a book only its owner may see
    assert client.post("/api/clip/answer", headers=h(token(BOB)), json={**body, "book_id": "u-alice-1"}).status_code == 404


def test_route_reads_nothing_without_a_free_slot_and_never_queues(tmp_path, monkeypatch):
    import asyncio
    import threading
    from app.routes import clip
    client, _, face, folder = route_env(tmp_path, monkeypatch)
    clip_path(face, folder).write_bytes(fmp4([[b"v"]], [[b"a"]]))
    reads = []
    real_read, real_inputs = clip._read_all, clip._lipsync_inputs
    monkeypatch.setattr(clip, "_read_all", lambda paths: (reads.append("clips"), real_read(paths))[1])
    monkeypatch.setattr(clip, "_lipsync_inputs", lambda r: (reads.append("face"), real_inputs(r))[1])
    body = {"book_id": "alice", "sentences": [SENT]}
    # all slots taken: 429 at once, and no clip or face image was read
    monkeypatch.setattr(clip, "_joins", asyncio.Semaphore(0))
    assert client.post("/api/clip/answer", json=body).status_code == 429 and reads == []
    # one slot, held by a join in progress: a second request is a 429, not a wait, and reads nothing
    monkeypatch.setattr(clip, "_joins", asyncio.Semaphore(1))
    entered, release = threading.Event(), threading.Event()
    real_join = mp4join.join_clips

    def slow_join(data):
        entered.set()
        release.wait(10)
        return real_join(data)
    monkeypatch.setattr(mp4join, "join_clips", slow_join)
    first = {}
    t = threading.Thread(target=lambda: first.setdefault("r", client.post("/api/clip/answer", json=body)))
    t.start()
    assert entered.wait(10)
    assert client.post("/api/clip/answer", json=body).status_code == 429
    assert reads == ["face", "clips"]  # only the running join's
    release.set()
    t.join(10)
    assert first["r"].status_code == 200
    assert client.post("/api/clip/answer", json=body).status_code == 200  # the slot is free again
