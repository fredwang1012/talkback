"""Shareable answer clips (#80) in pure Python: MP4 files built without ffmpeg (not in the container image).

- `join_clips`: the per-sentence lip-sync clips of one answer (one H.264 video and one AAC audio track each, the same
  encoder settings) become one ordinary (progressive) MP4. Boson's stream gives fragmented MP4, its download a
  progressive one (with B-frames and an edit list); both are read. No re-encoding: the samples are copied as they are
  and only the index tables are written anew. Each clip's video is fitted to the start of the next clip's voice (edit
  lists honoured), so voice and face stay in step across the joins.
- `still_mp4`: an answer with no face: the sentences' cached MP3s with a still picture (the portrait or letter circle
  as the reader saw it, sent as raw I420 pixels). The picture is coded as H.264 I_PCM (uncompressed macroblocks, no
  encoder needed), held by skipped frames; MP3 goes into MP4 as is (object type 0x69 for MPEG-2/2.5 audio, as the
  cached 24 kHz TTS is; 0x6B for MPEG-1).

Everything here works on bytes the caller has already bounded (clip count and size); any malformed input raises
Mp4Error, never a partial file.
"""
import math
import struct
from dataclasses import dataclass, field

from . import mp3dur


class Mp4Error(ValueError):
    """The input cannot be joined (malformed, an unsupported layout, or clips in different formats)."""


# ---------------------------------------------------------------------------------------------------------------
# reading (progressive or fragmented MP4)

def _boxes(data: bytes, start: int, end: int):
    """(type, box start, content start, box end) of each box in data[start:end]."""
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, off)
        hdr = 8
        if size == 1:
            if off + 16 > end:
                raise Mp4Error("truncated box")
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            raise Mp4Error("bad box size")
        yield typ.decode("latin-1"), off, off + hdr, off + size
        off += size


def _child(data: bytes, start: int, end: int, typ: str):
    return next((b for b in _boxes(data, start, end) if b[0] == typ), None)


def _need(data: bytes, start: int, end: int, *path: str) -> tuple[str, int, int, int]:
    box = None
    for typ in path:
        box = _child(data, start, end, typ)
        if box is None:
            raise Mp4Error(f"missing {typ}")
        start, end = box[2], box[3]
    return box  # type: ignore[return-value]


@dataclass
class _Track:
    track_id: int
    handler: str
    timescale: int
    stsd: bytes
    width: int  # 16.16 fixed point, as in tkhd
    height: int
    trex: tuple[int, int, int] = (0, 0, 0)  # default sample duration, size, flags
    media_time: int = 0  # the edit list's start in the media (B-frame delay, AAC priming); presentation = CT - this
    sizes: list[int] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    ctos: list[int] = field(default_factory=list)
    syncs: list[bool] = field(default_factory=list)
    data: list[bytes] = field(default_factory=list)


def _u24(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 3], "big")


def _edit_start(data: bytes, cs: int, ce: int) -> int:
    """media_time of a track's edit list (0 without one). Only the plain form encoders write is accepted: one edit that
    plays the media from some start at normal rate; anything else (gaps, several pieces) would need re-timing."""
    edts = _child(data, cs, ce, "edts")
    elst = edts and _child(data, edts[2], edts[3], "elst")
    if not elst:
        return 0
    v, n, p = data[elst[2]], struct.unpack_from(">I", data, elst[2] + 4)[0], elst[2] + 8
    if n == 0:
        return 0
    if n != 1 or p + (20 if v else 12) > elst[3]:
        raise Mp4Error("unsupported edit list")
    _seg, start, rate = struct.unpack_from(">QqI" if v else ">IiI", data, p)
    if start < 0 or rate != 0x10000:
        raise Mp4Error("unsupported edit list")
    return start


def _table(data: bytes, box, width: int, fmt: str) -> list[tuple]:
    """The entries of a full box that is a count followed by fixed-size entries (stts, ctts, stsc, stco, ...)."""
    _, _, cs, ce = box
    n = struct.unpack_from(">I", data, cs + 4)[0]
    if n * width > ce - cs - 8:
        raise Mp4Error("bad sample table")
    return [struct.unpack_from(fmt, data, cs + 8 + i * width) for i in range(n)]


def _read_stbl(data: bytes, track: _Track, s: int, e: int) -> None:
    """The samples a progressive MP4 lists in the track's sample tables (none in a fragmented MP4's init segment)."""
    stsz = _child(data, s, e, "stsz")
    if stsz is None:
        if _child(data, s, e, "stz2"):
            raise Mp4Error("unsupported sample size table")
        return
    size, count = struct.unpack_from(">II", data, stsz[2] + 4)
    if not count:
        return
    if size:
        if count * size > len(data):
            raise Mp4Error("sample outside the file")
        sizes = [size] * count
    else:
        if count * 4 > stsz[3] - stsz[2] - 12:
            raise Mp4Error("bad sample table")
        sizes = list(struct.unpack_from(f">{count}I", data, stsz[2] + 12))
    durations: list[int] = []
    for n, d in _table(data, _need(data, s, e, "stts"), 8, ">II"):
        if len(durations) + n > count:
            raise Mp4Error("bad sample table")
        durations += [d] * n
    ctos = [0] * count
    ctts = _child(data, s, e, "ctts")
    if ctts:
        ctos = []
        for n, c in _table(data, ctts, 8, ">Ii" if data[ctts[2]] else ">II"):
            if len(ctos) + n > count:
                raise Mp4Error("bad sample table")
            ctos += [c] * n
    stss = _child(data, s, e, "stss")
    syncs = [True] * count
    if stss:
        syncs = [False] * count
        for (i,) in _table(data, stss, 4, ">I"):
            if not 1 <= i <= count:
                raise Mp4Error("bad sample table")
            syncs[i - 1] = True
    stco, co64 = _child(data, s, e, "stco"), _child(data, s, e, "co64")
    offsets = [o for (o,) in (_table(data, stco, 4, ">I") if stco else _table(data, co64, 8, ">Q") if co64 else [])]
    stsc = _table(data, _need(data, s, e, "stsc"), 12, ">III")
    if len(durations) != count or len(ctos) != count or not stsc or stsc[0][0] != 1:
        raise Mp4Error("bad sample table")
    i = 0
    for k, (first, per, desc) in enumerate(stsc):
        last = stsc[k + 1][0] - 1 if k + 1 < len(stsc) else len(offsets)
        if desc != 1:
            raise Mp4Error("more than one sample description")
        if last > len(offsets) or last < first - 1:
            raise Mp4Error("bad sample table")
        for chunk in range(first - 1, last):
            pos = offsets[chunk]
            for _ in range(min(per, count - i)):
                if pos + sizes[i] > len(data):
                    raise Mp4Error("sample outside the file")
                track.data.append(bytes(data[pos:pos + sizes[i]]))
                pos += sizes[i]
                i += 1
    if i != count:
        raise Mp4Error("bad sample table")
    track.sizes, track.durations, track.ctos, track.syncs = sizes, durations, ctos, syncs


def parse_clip(data: bytes) -> dict[str, _Track]:
    """The tracks of a progressive or fragmented MP4 by handler ("vide", "soun"), with every sample's bytes and timing."""
    try:
        return _parse(data)
    except (struct.error, IndexError):
        raise Mp4Error("truncated")


def _parse(data: bytes) -> dict[str, _Track]:
    _, _, ms, me = _need(data, 0, len(data), "moov")
    tracks: dict[int, _Track] = {}
    for typ, _, cs, ce in _boxes(data, ms, me):
        if typ != "trak":
            continue
        _, _, ts, _ = _need(data, cs, ce, "tkhd")
        v1 = data[ts] == 1
        track_id = struct.unpack_from(">I", data, ts + (20 if v1 else 12))[0]
        width, height = struct.unpack_from(">II", data, ts + (88 if v1 else 76))
        _, _, hs, _ = _need(data, cs, ce, "mdia", "mdhd")
        timescale = struct.unpack_from(">I", data, hs + (20 if data[hs] == 1 else 12))[0]
        _, _, ds, _ = _need(data, cs, ce, "mdia", "hdlr")
        handler = data[ds + 8:ds + 12].decode("latin-1")
        _, _, bs, be = _need(data, cs, ce, "mdia", "minf", "stbl")
        _, s0, _, s1 = _need(data, bs, be, "stsd")
        if not timescale:
            raise Mp4Error("zero timescale")
        track = _Track(track_id, handler, timescale, bytes(data[s0:s1]), width, height, media_time=_edit_start(data, cs, ce))
        _read_stbl(data, track, bs, be)
        tracks[track_id] = track
    mvex = _child(data, ms, me, "mvex")
    if mvex:
        for typ, _, cs, _ in _boxes(data, mvex[2], mvex[3]):
            if typ == "trex":
                tid, _sdi, dur, size, flags = struct.unpack_from(">IIIII", data, cs + 4)
                if tid in tracks:
                    tracks[tid].trex = (dur, size, flags)

    for typ, mo, cs, ce in _boxes(data, 0, len(data)):
        if typ != "moof":
            continue
        prev_end, first_traf = mo, True
        for t2, _, ts, te in _boxes(data, cs, ce):
            if t2 != "traf":
                continue
            _, _, fs, _ = _need(data, ts, te, "tfhd")
            flags, tid = _u24(data, fs + 1), struct.unpack_from(">I", data, fs + 4)[0]
            track = tracks.get(tid)
            if track is None:
                raise Mp4Error("fragment of an unknown track")
            p = fs + 8
            base = None
            if flags & 0x1:
                base = struct.unpack_from(">Q", data, p)[0]
                p += 8
            if flags & 0x2:
                if struct.unpack_from(">I", data, p)[0] != 1:
                    raise Mp4Error("more than one sample description")
                p += 4
            dur, size, sflags = track.trex
            if flags & 0x8:
                dur = struct.unpack_from(">I", data, p)[0]
                p += 4
            if flags & 0x10:
                size = struct.unpack_from(">I", data, p)[0]
                p += 4
            if flags & 0x20:
                sflags = struct.unpack_from(">I", data, p)[0]
            if base is None:
                base = mo if (flags & 0x20000 or first_traf) else prev_end  # ISO 14496-12 8.8.7.1
            pos = base
            for t3, _, rs, _ in _boxes(data, ts, te):
                if t3 != "trun":
                    continue
                version, rflags = data[rs], _u24(data, rs + 1)
                count = struct.unpack_from(">I", data, rs + 4)[0]
                q = rs + 8
                if rflags & 0x1:
                    pos = base + struct.unpack_from(">i", data, q)[0]
                    q += 4
                first = None
                if rflags & 0x4:
                    first = struct.unpack_from(">I", data, q)[0]
                    q += 4
                for i in range(count):
                    d, s, f, c = dur, size, sflags, 0
                    if rflags & 0x100:
                        d = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    if rflags & 0x200:
                        s = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    if rflags & 0x400:
                        f = struct.unpack_from(">I", data, q)[0]
                        q += 4
                    elif i == 0 and first is not None:
                        f = first
                    if rflags & 0x800:
                        c = struct.unpack_from(">i" if version else ">I", data, q)[0]
                        q += 4
                    if s <= 0 or pos < 0 or pos + s > len(data):
                        raise Mp4Error("sample outside the file")
                    track.sizes.append(s)
                    track.durations.append(d)
                    track.ctos.append(c)
                    track.syncs.append(not f & 0x10000)
                    track.data.append(bytes(data[pos:pos + s]))
                    pos += s
            prev_end, first_traf = pos, False
    return {t.handler: t for t in tracks.values()}


# ---------------------------------------------------------------------------------------------------------------
# writing (progressive MP4)

def _box(typ: str, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), typ.encode("latin-1")) + body


def _full(typ: str, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(typ, struct.pack(">I", (version << 24) | flags), *parts)


_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


@dataclass
class OutTrack:
    handler: str  # "vide" or "soun"
    timescale: int
    stsd: bytes  # the whole stsd box
    durations: list[int]
    width: int = 0  # 16.16
    height: int = 0
    ctos: list[int] | None = None  # composition offsets; None = all zero
    syncs: list[bool] | None = None  # None = every sample is a sync sample
    edit: tuple[int, int] | None = None  # (duration, media start) in the track's timescale; None = no edit list


def _runs(values: list[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for v in values:
        if out and out[-1][1] == v:
            out[-1][0] += 1
        else:
            out.append([1, v])
    return [(n, v) for n, v in out]


def _stbl(t: OutTrack, sizes: list[int], chunk_counts: list[int], offsets: list[int]) -> bytes:
    stts = _runs(t.durations)
    parts = [t.stsd, _full("stts", 0, 0, struct.pack(">I", len(stts)), *(struct.pack(">II", n, d) for n, d in stts))]
    if t.ctos and any(t.ctos):
        ctts = _runs(t.ctos)
        neg = any(c < 0 for c in t.ctos)
        parts.append(_full("ctts", 1 if neg else 0, 0, struct.pack(">I", len(ctts)),
                           *(struct.pack(">Ii" if neg else ">II", n, c) for n, c in ctts)))
    if t.syncs is not None and not all(t.syncs):
        idx = [i + 1 for i, s in enumerate(t.syncs) if s]
        parts.append(_full("stss", 0, 0, struct.pack(">I", len(idx)), *(struct.pack(">I", i) for i in idx)))
    stsc: list[tuple[int, int]] = []
    for i, n in enumerate(chunk_counts):
        if not stsc or stsc[-1][1] != n:
            stsc.append((i + 1, n))
    parts.append(_full("stsc", 0, 0, struct.pack(">I", len(stsc)), *(struct.pack(">III", c, n, 1) for c, n in stsc)))
    parts.append(_full("stsz", 0, 0, struct.pack(">II", 0, len(sizes)), struct.pack(f">{len(sizes)}I", *sizes)))
    if offsets and offsets[-1] > 0xFFFFFFFF:
        raise Mp4Error("clip too large")
    parts.append(_full("stco", 0, 0, struct.pack(">I", len(offsets)), struct.pack(f">{len(offsets)}I", *offsets)))
    return _box("stbl", *parts)


def _moov(tracks: list[OutTrack], sizes: list[list[int]], counts: list[list[int]], offsets: list[list[int]]) -> bytes:
    def movie_dur(t: OutTrack) -> int:
        return (t.edit[0] if t.edit else sum(t.durations)) * 1000 // t.timescale
    total = max((movie_dur(t) for t in tracks), default=0)
    mvhd = _full("mvhd", 0, 0, struct.pack(">IIII", 0, 0, 1000, total), struct.pack(">IH10x", 0x10000, 0x100), _MATRIX,
                 b"\0" * 24, struct.pack(">I", len(tracks) + 1))
    traks = []
    for i, t in enumerate(tracks):
        video = t.handler == "vide"
        tkhd = _full("tkhd", 0, 3, struct.pack(">IIIII", 0, 0, i + 1, 0, movie_dur(t)), b"\0" * 8,
                     struct.pack(">HHHH", 0, 0, 0 if video else 0x100, 0), _MATRIX, struct.pack(">II", t.width, t.height))
        mdhd = _full("mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, t.timescale, sum(t.durations), 0x55C4, 0))
        name = b"VideoHandler\0" if video else b"SoundHandler\0"
        hdlr = _full("hdlr", 0, 0, struct.pack(">I4s12x", 0, t.handler.encode("latin-1")), name)
        head = _full("vmhd", 0, 1, b"\0" * 8) if video else _full("smhd", 0, 0, b"\0" * 4)
        dinf = _box("dinf", _full("dref", 0, 0, struct.pack(">I", 1), _full("url ", 0, 1)))
        minf = _box("minf", head, dinf, _stbl(t, sizes[i], counts[i], offsets[i]))
        # an edit list: the media plays from edit[1] (B-frame delay or AAC priming skipped), as in the source clips
        edts = [_box("edts", _full("elst", 0, 0, struct.pack(">IIiI", 1, movie_dur(t), t.edit[1], 0x10000)))] if t.edit else []
        traks.append(_box("trak", tkhd, *edts, _box("mdia", mdhd, hdlr, minf)))
    return _box("moov", mvhd, *traks)


FTYP = _box("ftyp", b"isom", struct.pack(">I", 0x200), b"isom", b"iso2", b"avc1", b"mp41")


def write_mp4(tracks: list[OutTrack], chunks: list[tuple[int, list[bytes]]]) -> bytes:
    """One progressive MP4: `chunks` are (track index, the samples' bytes) in file order (interleaved)."""
    sizes: list[list[int]] = [[] for _ in tracks]
    counts: list[list[int]] = [[] for _ in tracks]
    rel: list[list[int]] = [[] for _ in tracks]
    pos = 0
    for ti, samples in chunks:
        if not samples:
            continue
        rel[ti].append(pos)
        counts[ti].append(len(samples))
        for s in samples:
            sizes[ti].append(len(s))
            pos += len(s)
    for t, s in zip(tracks, sizes):
        if len(s) != len(t.durations):
            raise Mp4Error("sample count mismatch")
    head = len(FTYP) + len(_moov(tracks, sizes, counts, rel)) + 8  # stco entries are fixed size: same length again
    moov = _moov(tracks, sizes, counts, [[head + o for o in r] for r in rel])
    if len(FTYP) + len(moov) + 8 != head:
        raise Mp4Error("layout changed")
    mdat = struct.pack(">I4s", 8 + pos, b"mdat")
    return b"".join([FTYP, moov, mdat, *(s for _, samples in chunks for s in samples)])


# ---------------------------------------------------------------------------------------------------------------
# joining lip-sync clips

def _descriptors(b: bytes, off: int, end: int):
    """(tag, body start, body end) of the MPEG-4 descriptors in b[off:end] (esds)."""
    while off + 2 <= end:
        tag, n, off = b[off], 0, off + 1
        for _ in range(4):
            n, off = (n << 7) | (b[off] & 0x7F), off + 1
            if not b[off - 1] & 0x80:
                break
        if off + n > end:
            raise Mp4Error("bad esds")
        yield tag, off, off + n
        off += n


def _esds_key(b: bytes) -> tuple:
    """What decodes an esds: object type, stream type and decoder config, not its (per-file) bitrate fields."""
    for tag, s, e in _descriptors(b, 4, len(b)):
        if tag != 0x03:
            continue
        flags, off = b[s + 2], s + 3
        off += (2 if flags & 0x80 else 0) + (2 if flags & 0x20 else 0)
        if flags & 0x40:
            off += 1 + b[off]
        for t2, s2, e2 in _descriptors(b, off, e):
            if t2 == 0x04 and e2 - s2 >= 13:
                dsi = next((b[s3:e3] for t3, s3, e3 in _descriptors(b, s2 + 13, e2) if t3 == 0x05), b"")
                return b[s2], b[s2 + 1] >> 2, dsi
    raise Mp4Error("bad esds")


def _format_key(handler: str, stsd: bytes) -> tuple:
    """The parts of a track's sample description a decoder depends on. Two clips with equal keys can share one sample
    description; per-file statistics (btrt, the esds bitrates, the encoder name) are left out, since an encoder writes
    them differently for every clip."""
    entries = list(_boxes(stsd, 16, len(stsd)))
    if struct.unpack_from(">I", stsd, 12)[0] != 1 or len(entries) != 1:
        raise Mp4Error("more than one sample description")
    typ, _, cs, ce = entries[0]
    if ce - cs < (78 if handler == "vide" else 28):
        return typ, stsd[cs:ce]  # not a sample entry this knows: compared as it is
    if handler == "vide":
        fixed, kids = stsd[cs + 24:cs + 28], cs + 78  # width, height
    else:
        version = struct.unpack_from(">H", stsd, cs + 8)[0]
        fixed, kids = stsd[cs + 8:cs + 28], cs + 28 + {0: 0, 1: 16, 2: 36}.get(version, 0)  # channels, sample size, rate
    try:
        children = tuple((t, _esds_key(stsd[s:e]) if t == "esds" else stsd[s:e])
                         for t, _, s, e in _boxes(stsd, kids, ce) if t != "btrt")
    except IndexError:
        raise Mp4Error("bad esds")
    return typ, fixed, children


def join_clips(clips: list[bytes]) -> bytes:
    """The sentence clips of one answer, in order, as one MP4 (video and voice from the clips, no re-encoding)."""
    if not clips:
        raise Mp4Error("no clips")
    parsed = [parse_clip(c) for c in clips]
    ref = parsed[0]
    if "vide" not in ref or "soun" not in ref:
        raise Mp4Error("a clip has no video or no audio")
    v_ref, a_ref = ref["vide"], ref["soun"]
    keys = {h: _format_key(h, r.stsd) for h, r in (("vide", v_ref), ("soun", a_ref))}
    for p in parsed:
        for h, r in (("vide", v_ref), ("soun", a_ref)):
            t = p.get(h)
            if t is None or t.timescale != r.timescale or _format_key(h, t.stsd) != keys[h]:
                raise Mp4Error("the sentence clips are not in the same format")
            if not t.sizes:
                raise Mp4Error("an empty clip")
    vts, ats = v_ref.timescale, a_ref.timescale
    # Presentation time = composition time - the edit's start. B-frame clips start late by their reorder delay (their
    # edit skips it): one shared start for the joined video, each clip's offsets raised to it, keeps every frame where
    # its clip showed it. The audio keeps the first clip's start (priming); a later clip's priming plays as the short
    # silence it is, and that clip's face waits for the voice behind it.
    shift = max(p["vide"].media_time for p in parsed)
    a_start = ref["soun"].media_time
    for p in parsed:
        v = p["vide"]
        if any(cto + i_dts < v.media_time for cto, i_dts in zip(v.ctos, _dts(v.durations))):
            raise Mp4Error("unsupported edit list")  # frames the clip never shows
    voice_at = []  # where each clip's voice starts in the joined audio's presentation (audio timescale), then its end
    cum_a = 0
    for p in parsed:
        voice_at.append(cum_a + p["soun"].media_time - a_start)
        cum_a += sum(p["soun"].durations)
    voice_at.append(cum_a - a_start)
    if any(b <= a for a, b in zip(voice_at, voice_at[1:])):
        raise Mp4Error("a clip with no voice")
    vd: list[int] = []
    ad: list[int] = []
    vc: list[int] = []
    vs: list[bool] = []
    chunks: list[tuple[int, list[bytes]]] = []
    cum_v = 0
    for i, p in enumerate(parsed):
        v, a = p["vide"], p["soun"]
        # this clip's frames run until the next clip's voice starts (the last clip's: until the voice ends)
        end = (voice_at[i + 1] * vts + ats // 2) // ats - cum_v
        dts = _dts(v.durations)
        n = len(dts)
        # frames the clip has after its voice would cover the next sentence: leave them out (the tail in decoding
        # order: nothing kept refers to them)
        while n > 1 and (dts[n - 1] >= end or dts[n - 1] + v.ctos[n - 1] - v.media_time >= end):
            n -= 1
        durs = v.durations[:n]
        durs[-1] = max(1, end - dts[n - 1])  # the last frame is held (or cut) to meet the next voice
        cum_v += dts[n - 1] + durs[-1]
        vd += durs
        vc += [c + shift - v.media_time for c in v.ctos[:n]]
        vs += v.syncs[:n]
        ad += a.durations
        chunks += [(0, v.data[:n]), (1, a.data)]
    video = OutTrack("vide", vts, v_ref.stsd, vd, v_ref.width, v_ref.height, vc, vs,
                     (sum(vd), shift) if shift else None)
    audio = OutTrack("soun", ats, a_ref.stsd, ad, edit=(sum(ad) - a_start, a_start) if a_start else None)
    return write_mp4([video, audio], chunks)


def _dts(durations: list[int]) -> list[int]:
    out, t = [], 0
    for d in durations:
        out.append(t)
        t += d
    return out


# ---------------------------------------------------------------------------------------------------------------
# a still picture with the answer's MP3 audio

class _Bits:
    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def u(self, n: int, v: int) -> None:
        for i in range(n - 1, -1, -1):
            self.acc = (self.acc << 1) | ((v >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.acc)
                self.acc = self.n = 0

    def ue(self, v: int) -> None:
        x = v + 1
        self.u(x.bit_length() - 1, 0)
        self.u(x.bit_length(), x)

    def se(self, v: int) -> None:
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def align_zero(self) -> None:
        while self.n:
            self.u(1, 0)

    def raw(self, b: bytes) -> None:
        assert self.n == 0
        self.out += b

    def trailing(self) -> bytes:
        self.u(1, 1)
        self.align_zero()
        return bytes(self.out)


def _nal(ref_idc: int, typ: int, rbsp: bytes) -> bytes:
    """A NAL unit (no start code) with emulation prevention bytes."""
    out = bytearray([(ref_idc << 5) | typ])
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


STILL_MAX_SIDE = 640
_LEVEL = 30  # 3.0: up to 1620 macroblocks per frame (640x640 = 1600)


def _sps(w: int, h: int) -> bytes:
    b = _Bits()
    b.u(8, 66)  # Baseline
    b.u(8, 0b11000000)  # constraint_set0/1: decodable by Baseline and Main decoders
    b.u(8, _LEVEL)
    b.ue(0)  # seq_parameter_set_id
    b.ue(0)  # log2_max_frame_num_minus4: frame_num 0..15
    b.ue(2)  # pic_order_cnt_type 2: output order = decoding order
    b.ue(1)  # max_num_ref_frames
    b.u(1, 0)  # gaps_in_frame_num_value_allowed_flag
    b.ue(w // 16 - 1)
    b.ue(h // 16 - 1)
    b.u(1, 1)  # frame_mbs_only_flag
    b.u(1, 1)  # direct_8x8_inference_flag
    b.u(1, 0)  # frame_cropping_flag
    b.u(1, 0)  # vui_parameters_present_flag
    return _nal(3, 7, b.trailing())


def _pps() -> bytes:
    b = _Bits()
    b.ue(0)  # pic_parameter_set_id
    b.ue(0)  # seq_parameter_set_id
    b.u(1, 0)  # entropy_coding_mode_flag: CAVLC
    b.u(1, 0)  # bottom_field_pic_order_in_frame_present_flag
    b.ue(0)  # num_slice_groups_minus1
    b.ue(0)
    b.ue(0)  # num_ref_idx_l0/l1_default_active_minus1
    b.u(1, 0)  # weighted_pred_flag
    b.u(2, 0)  # weighted_bipred_idc
    b.se(0)
    b.se(0)
    b.se(0)  # pic_init_qp/qs_minus26, chroma_qp_index_offset
    b.u(1, 1)  # deblocking_filter_control_present_flag
    b.u(1, 0)  # constrained_intra_pred_flag
    b.u(1, 0)  # redundant_pic_cnt_present_flag
    return _nal(3, 8, b.trailing())


_NO_ZERO = bytes([1] + list(range(1, 256)))  # PCM samples of 0 were not allowed in early H.264 profiles


def _idr(i420: bytes, w: int, h: int, idr_id: int) -> bytes:
    """An IDR frame of I_PCM macroblocks: the picture exactly, uncompressed."""
    b = _Bits()
    b.ue(0)  # first_mb_in_slice
    b.ue(7)  # slice_type: I (all slices)
    b.ue(0)  # pic_parameter_set_id
    b.u(4, 0)  # frame_num
    b.ue(idr_id)  # idr_pic_id
    b.u(1, 0)
    b.u(1, 0)  # dec_ref_pic_marking: no_output_of_prior_pics, long_term_reference
    b.se(0)  # slice_qp_delta
    b.ue(1)  # disable_deblocking_filter_idc: off
    pix = i420.translate(_NO_ZERO)
    cw, ch = w // 2, h // 2
    u_at, v_at = w * h, w * h + cw * ch
    for my in range(h // 16):
        for mx in range(w // 16):
            b.ue(25)  # mb_type I_PCM
            b.align_zero()
            b.raw(b"".join(pix[(my * 16 + r) * w + mx * 16:(my * 16 + r) * w + mx * 16 + 16] for r in range(16)))
            for at in (u_at, v_at):
                b.raw(b"".join(pix[at + (my * 8 + r) * cw + mx * 8:at + (my * 8 + r) * cw + mx * 8 + 8] for r in range(8)))
    return _nal(3, 5, b.trailing())


def _skip(w: int, h: int, frame_num: int) -> bytes:
    """A P frame that repeats the previous one (every macroblock skipped): a few bytes."""
    b = _Bits()
    b.ue(0)
    b.ue(5)  # slice_type: P (all slices)
    b.ue(0)
    b.u(4, frame_num % 16)
    b.u(1, 0)  # num_ref_idx_active_override_flag
    b.u(1, 0)  # ref_pic_list_modification_flag_l0
    b.u(1, 0)  # adaptive_ref_pic_marking_mode_flag
    b.se(0)
    b.ue(1)
    b.ue((w // 16) * (h // 16))  # mb_skip_run: the whole picture
    return _nal(2, 1, b.trailing())


def _avc1(w: int, h: int, sps: bytes, pps: bytes) -> bytes:
    avcc = _box("avcC", bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]), struct.pack(">H", len(sps)), sps,
                b"\x01", struct.pack(">H", len(pps)), pps)
    entry = _box("avc1", b"\0" * 6, struct.pack(">H", 1), b"\0" * 16, struct.pack(">HHII", w, h, 0x480000, 0x480000),
                 b"\0" * 4, struct.pack(">H", 1), b"\0" * 32, struct.pack(">Hh", 0x18, -1), avcc)
    return _full("stsd", 0, 0, struct.pack(">I", 1), entry)


def _descr(tag: int, body: bytes) -> bytes:
    n = len(body)
    return bytes([tag, 0x80 | (n >> 21 & 0x7F), 0x80 | (n >> 14 & 0x7F), 0x80 | (n >> 7 & 0x7F), n & 0x7F]) + body


def _mp4a_mp3(rate: int, channels: int, bitrate: int, mpeg1: bool) -> bytes:
    # objectTypeIndication: 0x6B is ISO 11172-3 (MPEG-1 audio, 32-48 kHz), 0x69 ISO 13818-3 (MPEG-2 audio: the half
    # rates, 16-24 kHz, and in practice MPEG-2.5 too); players that check it mute or reject a mislabelled stream
    dcd = _descr(0x04, bytes([0x6B if mpeg1 else 0x69, 0x15]) + (0).to_bytes(3, "big") + struct.pack(">II", bitrate, bitrate))
    esd = _descr(0x03, struct.pack(">HB", 1, 0) + dcd + _descr(0x06, b"\x02"))
    entry = _box("mp4a", b"\0" * 6, struct.pack(">H", 1), b"\0" * 8, struct.pack(">HHHH", channels, 16, 0, 0),
                 struct.pack(">I", rate << 16), _full("esds", 0, 0, esd))
    return _full("stsd", 0, 0, struct.pack(">I", 1), entry)


def mp3_frames(data: bytes) -> tuple[list[bytes], int, int, int, int]:
    """(frames, sample rate, samples per frame, channels, bitrate of the first frame) of a CBR or VBR Layer III file."""
    bare = mp3dur._bare(data)
    head = mp3dur._first_frame(bare)
    if head is None:
        raise Mp4Error("not an MP3")
    off, bitrate, rate, spf, ch = head
    frames = []
    while off < len(bare):
        n = mp3dur._frame_len(bare, off)
        if not n or off + n > len(bare):
            break  # a trailing partial frame or tag
        frames.append(bytes(bare[off:off + n]))
        off += n
    if not frames:
        raise Mp4Error("not an MP3")
    return frames, rate, spf, ch, bitrate


IDR_EVERY = 10  # frames (seconds): a full picture this often, so seeking lands on one


def still_mp4(i420: bytes, width: int, height: int, mp3s: list[bytes]) -> bytes:
    """The answer's MP3s in order with a still picture (I420, width x height, multiples of 16) as an MP4."""
    if width % 16 or height % 16 or not (16 <= width <= STILL_MAX_SIDE and 16 <= height <= STILL_MAX_SIDE):
        raise Mp4Error("bad picture size")
    if len(i420) != width * height * 3 // 2:
        raise Mp4Error("bad picture data")
    if not mp3s:
        raise Mp4Error("no audio")
    frames: list[bytes] = []
    fmt = None
    for m in mp3s:
        f, rate, spf, ch, bitrate = mp3_frames(m)
        if fmt is None:
            fmt = (rate, spf, ch, bitrate)
        elif fmt[:3] != (rate, spf, ch):
            raise Mp4Error("the sentences' audio is not in the same format")
        frames += f
    rate, spf, ch, bitrate = fmt  # type: ignore[misc]
    total_ms = max(1, len(frames) * spf * 1000 // rate)
    n_video = math.ceil(total_ms / 1000)
    vd = [1000] * n_video
    vd[-1] = total_ms - 1000 * (n_video - 1)
    sps, pps = _sps(width, height), _pps()
    with4 = lambda nal: struct.pack(">I", len(nal)) + nal  # noqa: E731  (length-prefixed NAL, avcC lengthSize 4)
    idrs: dict[int, bytes] = {}  # neighbouring IDRs differ only in idr_pic_id: each of the two is coded once
    chunks: list[tuple[int, list[bytes]]] = []
    syncs: list[bool] = []
    per_second = rate / spf  # MP3 frames per second
    taken = 0
    for i in range(n_video):
        if i % IDR_EVERY == 0:
            idr_id = i // IDR_EVERY % 2
            if idr_id not in idrs:
                idrs[idr_id] = with4(_idr(i420, width, height, idr_id))
            sample = idrs[idr_id]
            syncs.append(True)
        else:
            sample = with4(_skip(width, height, i % IDR_EVERY))
            syncs.append(False)
        chunks.append((0, [sample]))
        upto = len(frames) if i == n_video - 1 else min(len(frames), round((i + 1) * per_second))
        chunks.append((1, frames[taken:upto]))
        taken = upto
    video = OutTrack("vide", 1000, _avc1(width, height, sps, pps), vd, width << 16, height << 16, None, syncs)
    mpeg1 = (frames[0][1] >> 3) & 3 == 3  # the header's version bits; the rate check above keeps one version
    audio = OutTrack("soun", rate, _mp4a_mp3(rate, ch, bitrate, mpeg1), [spf] * len(frames))
    return write_mp4([video, audio], chunks)
