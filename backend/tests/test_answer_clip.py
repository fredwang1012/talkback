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


def ptrak(tid, handler, timescale, entry, samples, dur, offset, ctos=None, start=None, syncs=None):
    """A progressive MP4's track: one chunk of `samples` at `offset`; optional ctts, stss and an edit list that
    starts the media at `start` (like Boson's download: B-frame delay 1024 on the video)."""
    tkhd = full("tkhd", 0, 3, struct.pack(">IIIII", 0, 0, tid, 0, 0), b"\0" * 52, struct.pack(">II", 64 << 16, 48 << 16))
    edts = [box("edts", full("elst", 0, 0, struct.pack(">IIiI", 1, 1000, start, 0x10000)))] if start is not None else []
    n = len(samples)
    tables = [full("stsd", 0, 0, struct.pack(">I", 1), entry), full("stts", 0, 0, struct.pack(">III", 1, n, dur))]
    if ctos:
        tables.append(full("ctts", 0, 0, struct.pack(">I", n), *(struct.pack(">II", 1, c) for c in ctos)))
    if syncs:
        tables.append(full("stss", 0, 0, struct.pack(">I", len(syncs)), *(struct.pack(">I", s) for s in syncs)))
    tables += [full("stsc", 0, 0, struct.pack(">IIII", 1, 1, n, 1)),
               full("stsz", 0, 0, struct.pack(">II", 0, n), *(struct.pack(">I", len(s)) for s in samples)),
               full("stco", 0, 0, struct.pack(">II", 1, offset))]
    mdhd = full("mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, n * dur, 0x55C4, 0))
    hdlr = full("hdlr", 0, 0, struct.pack(">I4s12x", 0, handler.encode()), b"x\0")
    return box("trak", tkhd, *edts, box("mdia", mdhd, hdlr, box("minf", box("stbl", *tables))))


def pmp4(video, audio, vdur=512, adur=1024, ventry=box("avc1", b"V1"), aentry=box("mp4a", b"A1"), ctos=None,
         vstart=None, astart=None, syncs=None):
    """A progressive MP4 (moov first, then one mdat: the video samples, then the audio)."""
    def build(base):
        return box("ftyp", b"isom", b"\0\0\2\0", b"isom") + box(
            "moov", full("mvhd", 0, 0, b"\0" * 96),
            ptrak(1, "vide", 12288, ventry, video, vdur, base, ctos, vstart, syncs),
            ptrak(2, "soun", 24000, aentry, audio, adur, base + sum(map(len, video)), None, astart))
    head = len(build(0)) + 8
    return build(head) + box("mdat", *video, *audio)


def avc1(tail=b""):
    """An avc1 sample entry (78 fixed bytes, then children) with an avcC and the given extra children."""
    return box("avc1", b"\0" * 6 + b"\0\1" + b"\0" * 16 + struct.pack(">HH", 64, 48) + b"\0" * 50,
               box("avcC", b"\1\x64\0\x1e\xff\xe1"), tail)


def mp4a(dsi=b"\x13\x08", bitrate=69000, channels=1, tail=b""):
    """An mp4a sample entry with an esds: AAC (0x40), its decoder config `dsi`, the (per-file) bitrate fields."""
    def d(tag, body):
        return bytes([tag, len(body)]) + body
    esds = full("esds", 0, 0, d(3, b"\0\2\0" + d(4, bytes([0x40, 0x15, 0, 0, 0]) + struct.pack(">II", bitrate, bitrate)
                                                + d(5, dsi)) + d(6, b"\2")))
    return box("mp4a", b"\0" * 6 + b"\0\1" + b"\0" * 8 + struct.pack(">HHHHI", channels, 16, 0, 0, 24000 << 16), esds, tail)


def presented(out):
    """Per output video sample: (presentation time, bytes), read back with the module's own reader."""
    v = mp4join.parse_clip(out)["vide"]
    t, times = 0, []
    for d, c in zip(v.durations, v.ctos):
        times.append(t + c - v.media_time)
        t += d
    return list(zip(times, v.data)), v


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
    p = mp4join.parse_clip(fmp4([[b"x1", b"yy2"]], [[b"zzz3"]]))
    assert p["vide"].data == [b"x1", b"yy2"] and p["vide"].syncs == [True, False] and p["vide"].timescale == 12288
    assert p["soun"].data == [b"zzz3"] and p["soun"].durations == [1024]


def test_join_reads_progressive_clips_and_ignores_per_file_bitrates():
    # Boson's download (/api/avatar/talk) is a progressive MP4 whose btrt and esds bitrates differ clip to clip
    a = pmp4([b"P0", b"p1"], [b"Q0"], ventry=avc1(box("btrt", b"\0" * 4 + struct.pack(">II", 1, 1))),
             aentry=mp4a(bitrate=63446))
    b = pmp4([b"R0"], [b"S0", b"s1"], ventry=avc1(box("btrt", b"\0" * 4 + struct.pack(">II", 9, 9))),
             aentry=mp4a(bitrate=70123))
    p = mp4join.parse_clip(a)
    assert p["vide"].data == [b"P0", b"p1"] and p["vide"].syncs == [True, True] and p["soun"].data == [b"Q0"]
    out = mp4join.join_clips([a, b])
    t = tree(out)
    cs, ce = t["mdat"][0]
    assert out[cs:ce] == b"P0p1Q0R0S0s1"
    # a progressive clip and a fragmented one of the same format join too
    assert mp4join.join_clips([a, fmp4([[b"F0"]], [[b"G0"]], ventry=avc1(), aentry=mp4a())])
    # a different decoder config, channel count or picture is a different format
    for other in (mp4a(dsi=b"\x11\x90"), mp4a(channels=2)):
        with pytest.raises(Mp4Error, match="same format"):
            mp4join.join_clips([a, pmp4([b"R0"], [b"S0"], ventry=avc1(), aentry=other)])
    with pytest.raises(Mp4Error, match="same format"):
        mp4join.join_clips([a, pmp4([b"R0"], [b"S0"], ventry=avc1(box("avcC", b"\1\x4d")), aentry=mp4a())])


def test_join_keeps_b_frame_order_and_the_edit_list():
    # decode order I P B, shown I B P after a 1024-tick delay the clip's edit list skips (as in Boson's download)
    a = pmp4([b"I", b"P", b"B"], [b"a0", b"a1"], ctos=[1024, 1536, 512], vstart=1024, syncs=[1])
    b = fmp4([[b"J", b"K"]], [[b"b0"]])  # no B-frames, no edit list
    out = mp4join.join_clips([a, b])
    shown, v = presented(out)
    assert v.media_time == 1024  # the joined file keeps the delay in its own edit list
    # clip one shows I B P from 0 every 512 ticks; clip two starts exactly where its voice does: 2048/24000 s = 1049
    assert sorted(shown) == [(0, b"I"), (512, b"B"), (1024, b"P"), (1049, b"J"), (1561, b"K")]
    assert sum(v.durations) == round(3072 * 12288 / 24000)  # the face ends with the voice
    t = tree(out)
    assert struct.unpack_from(">I", table(out, t, "stss", 0), 4)[0] == 2  # I and J


def test_join_starts_each_face_with_its_voice_after_an_audio_edit():
    # clip two's audio starts with 1024 samples of priming its edit list skips: its face waits for its voice
    a = fmp4([[b"v0", b"v1"]], [[b"a0", b"a1", b"a2"]])
    b = pmp4([b"w0", b"w1"], [b"b0", b"b1", b"b2"], astart=1024)
    shown, v = presented(mp4join.join_clips([a, b]))
    assert [s for s, _ in shown] == [0, 512, round(4096 * 12288 / 24000), round(4096 * 12288 / 24000) + 512]
    # the first clip's own priming edit becomes the joined audio's edit list
    out = mp4join.join_clips([b, a])
    p = mp4join.parse_clip(out)
    assert p["soun"].media_time == 1024 and p["vide"].media_time == 0
    shown, _ = presented(out)
    assert [s for s, _ in shown][2] == round(2048 * 12288 / 24000)  # clip two (a) starts where b's voice ended


def test_join_cuts_frames_that_run_past_their_voice():
    # 10 frames (5120 ticks) over 1024 samples of voice (524 ticks): frames from 524 on would show over the next voice
    a = fmp4([[b"f%d" % i for i in range(10)]], [[b"a0"]])
    b = fmp4([[b"g0"]], [[b"b0"]])
    out = mp4join.join_clips([a, b])
    shown, v = presented(out)
    assert shown == [(0, b"f0"), (512, b"f1"), (524, b"g0")] and v.durations == [512, 12, 525]  # ends at 2048 samples: 1049 ticks
    cs, ce = tree(out)["mdat"][0]
    assert out[cs:ce] == b"f0f1a0g0b0"


def test_join_refuses_edit_lists_it_cannot_keep():
    ok = pmp4([b"v"], [b"a"], vstart=0)
    empty_edit = ok.replace(struct.pack(">IIiI", 1, 1000, 0, 0x10000), struct.pack(">IIiI", 1, 1000, -1, 0x10000), 1)
    for bad in (pmp4([b"v"], [b"a"], vstart=512),  # starts after the only frame: a frame never shown
                empty_edit):  # a gap before the media
        with pytest.raises(Mp4Error, match="edit list"):
            mp4join.join_clips([bad])
    assert mp4join.join_clips([ok])


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
    assert b"mp4a" in stsd_a and bytes([0x69, 0x15]) in stsd_a  # MPEG-2 audio (the 24 kHz TTS) in MP4
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


def mp3_mpeg1(n_frames):
    """MPEG-1 Layer III, 64 kbit/s, 44100 Hz mono; 1152 samples per frame."""
    return b"".join(bytes([0xFF, 0xFB, 0x50, 0xC0]) + b"\0" * (144 * 64000 // 44100 - 4) for _ in range(n_frames))


def test_still_labels_the_mp3_with_its_own_mpeg_version():
    def oti(out):
        stsd = table(out, tree(out), "stsd", 1)
        at = stsd.index(b"\x04\x80\x80\x80") + 5  # DecoderConfigDescriptor: objectTypeIndication, streamType
        return stsd[at], stsd[at + 1]
    assert oti(mp4join.still_mp4(i420(), 32, 32, [mp3(10)])) == (0x69, 0x15)  # 24 kHz: ISO 13818-3
    assert oti(mp4join.still_mp4(i420(), 32, 32, [mp3(10, rate_idx=0)])) == (0x69, 0x15)  # 22.05 kHz
    assert oti(mp4join.still_mp4(i420(), 32, 32, [mp3_mpeg1(10)])) == (0x6B, 0x15)  # 44.1 kHz: ISO 11172-3


def test_still_codes_each_idr_once(monkeypatch):
    calls = []
    real = mp4join._idr
    monkeypatch.setattr(mp4join, "_idr", lambda *a: (calls.append(a[3]), real(*a))[1])
    out = mp4join.still_mp4(i420(), 32, 32, [mp3(int(35 * 24000 / 576))])  # 35 s: IDRs at 0, 10, 20, 30 s
    assert sorted(calls) == [0, 1]
    t = tree(out)
    stss = table(out, t, "stss", 0)
    assert struct.unpack_from(">5I", stss, 4) == (4, 1, 11, 21, 31)
    offs = struct.unpack_from(">35I", table(out, t, "stco", 0), 8)
    sizes = struct.unpack_from(">35I", table(out, t, "stsz", 0), 12)
    sample = [out[offs[i]:offs[i] + sizes[i]] for i in (0, 10, 20, 30)]
    assert sample[0] == sample[2] and sample[1] == sample[3] and sample[0] != sample[1]  # idr_pic_id alternates


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
    monkeypatch.setattr(clip, "_joins", threading.Semaphore(0))
    assert client.post("/api/clip/answer", json=body).status_code == 429 and reads == []
    # one slot, held by a join in progress: a second request is a 429, not a wait, and reads nothing
    monkeypatch.setattr(clip, "_joins", threading.BoundedSemaphore(1))
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


def test_route_keeps_the_slot_until_an_abandoned_join_really_ends(tmp_path, monkeypatch):
    """A request cancelled while its join runs (client gone, or the browser's 60 s abort) must not free its slot:
    the thread keeps working, so the slot is freed only when it is done."""
    import asyncio
    import threading
    from fastapi import HTTPException
    from app.routes import clip
    _client, _, face, folder = route_env(tmp_path, monkeypatch)
    clip_path(face, folder).write_bytes(fmp4([[b"v"]], [[b"a"]]))
    monkeypatch.setattr(deps, "rate_key", lambda request: "k")
    monkeypatch.setattr(clip, "_joins", type(clip._joins)(1))  # one slot
    entered, release = threading.Event(), threading.Event()
    real_join = mp4join.join_clips

    def slow_join(data):
        entered.set()
        release.wait(10)
        return real_join(data)
    monkeypatch.setattr(mp4join, "join_clips", slow_join)
    req = clip.ClipRequest(book_id="alice", sentences=[SENT])

    async def until(cond):
        for _ in range(500):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    async def scenario():
        first = asyncio.create_task(clip.answer_clip(req, None))
        await until(entered.is_set)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        try:
            # the abandoned join still runs: no slot for another
            with pytest.raises(HTTPException) as e:
                await asyncio.wait_for(clip.answer_clip(req, None), 2)
            assert e.value.status_code == 429
        finally:
            release.set()
        # once it ends, the slot is free again
        for _ in range(200):
            try:
                return await clip.answer_clip(req, None)
            except HTTPException as e:
                assert e.status_code == 429
                await asyncio.sleep(0.01)
        raise AssertionError("the slot was never freed")
    assert (asyncio.run(scenario())).status_code == 200


def test_route_spends_a_rate_limit_token_only_on_work(tmp_path, monkeypatch):
    from app.routes import clip
    client, _, face, folder = route_env(tmp_path, monkeypatch)
    clip_path(face, folder).write_bytes(fmp4([[b"v"]], [[b"a"]]))
    monkeypatch.setattr(deps, "clip_bucket", TokenBucket(1, 60))
    body = {"book_id": "alice", "sentences": [SENT]}
    # turned away before any work: a bad picture, uncached audio, all slots busy
    assert client.post("/api/clip/answer", json={**body, "still": "***"}).status_code == 400
    assert client.post("/api/clip/answer", json={**body, "still": base64.b64encode(b"x" * 10).decode()}).status_code == 400
    slots = clip._joins
    monkeypatch.setattr(clip, "_joins", type(slots)(0))
    r = client.post("/api/clip/answer", json=body)
    assert r.status_code == 429 and "Busy" in r.json()["detail"]
    monkeypatch.setattr(clip, "_joins", slots)
    # the one token is still there for a real download, and then it is spent
    assert client.post("/api/clip/answer", json=body).status_code == 200
    r = client.post("/api/clip/answer", json=body)
    assert r.status_code == 429 and "Too many" in r.json()["detail"]
