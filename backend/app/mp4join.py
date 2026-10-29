"""Shareable answer clips (#80) in pure Python: MP4 files built without ffmpeg (not in the container image).

- `join_clips`: the per-sentence lip-sync clips of one answer (Boson's fragmented MP4: one H.264 video and one AAC
  audio track each, the same encoder settings) become one ordinary (progressive) MP4. No re-encoding: the samples are
  copied as they are and only the index tables are written anew. Each clip's video is fitted to the end of its audio, so
  voice and face stay in step across the joins.
- `still_mp4`: an answer with no face: the sentences' cached MP3s with a still picture (the portrait or letter circle
  as the reader saw it, sent as raw I420 pixels). The picture is coded as H.264 I_PCM (uncompressed macroblocks, no
  encoder needed), held by skipped frames; MP3 goes into MP4 as is (object type 0x6B).

Everything here works on bytes the caller has already bounded (clip count and size); any malformed input raises
Mp4Error, never a partial file.
"""
import math
import struct
from dataclasses import dataclass, field

from . import mp3dur


class Mp4Error(ValueError):
    """The input cannot be joined (malformed, not fragmented, or clips in different formats)."""


# ---------------------------------------------------------------------------------------------------------------
# reading (fragmented MP4)

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
    sizes: list[int] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    ctos: list[int] = field(default_factory=list)
    syncs: list[bool] = field(default_factory=list)
    data: list[bytes] = field(default_factory=list)


def _u24(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 3], "big")


def parse_fragmented(data: bytes) -> dict[str, _Track]:
    """The tracks of a fragmented MP4 by handler ("vide", "soun"), with every sample's bytes and timing."""
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
        _, s0, _, s1 = _need(data, cs, ce, "mdia", "minf", "stbl", "stsd")
        stsz = _child(data, *_need(data, cs, ce, "mdia", "minf", "stbl")[2:4], "stsz")
        if stsz and struct.unpack_from(">I", data, stsz[2] + 8)[0]:
            raise Mp4Error("not a fragmented MP4")
        if not timescale:
            raise Mp4Error("zero timescale")
        tracks[track_id] = _Track(track_id, handler, timescale, bytes(data[s0:s1]), width, height)
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
        return sum(t.durations) * 1000 // t.timescale
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
        traks.append(_box("trak", tkhd, _box("mdia", mdhd, hdlr, minf)))
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

def join_clips(clips: list[bytes]) -> bytes:
    """The sentence clips of one answer, in order, as one MP4 (video and voice from the clips, no re-encoding)."""
    if not clips:
        raise Mp4Error("no clips")
    parsed = [parse_fragmented(c) for c in clips]
    ref = parsed[0]
    if "vide" not in ref or "soun" not in ref:
        raise Mp4Error("a clip has no video or no audio")
    v_ref, a_ref = ref["vide"], ref["soun"]
    for p in parsed:
        for h, r in (("vide", v_ref), ("soun", a_ref)):
            t = p.get(h)
            if t is None or t.stsd != r.stsd or t.timescale != r.timescale:
                raise Mp4Error("the sentence clips are not in the same format")
            if not t.sizes:
                raise Mp4Error("an empty clip")
    vts, ats = v_ref.timescale, a_ref.timescale
    vd: list[int] = []
    ad: list[int] = []
    vc: list[int] = []
    vs: list[bool] = []
    chunks: list[tuple[int, list[bytes]]] = []
    cum_a = cum_v = 0
    for p in parsed:
        v, a = p["vide"], p["soun"]
        durs = list(v.durations)
        cum_a += sum(a.durations)
        target = (cum_a * vts + ats // 2) // ats  # where this sentence's voice ends, in video time
        before_last = cum_v + sum(durs[:-1])
        durs[-1] = max(1, target - before_last)  # the last frame is held (or cut) to end with the voice
        cum_v = before_last + durs[-1]
        vd += durs
        vc += v.ctos
        vs += v.syncs
        ad += a.durations
        chunks += [(0, v.data), (1, a.data)]
    video = OutTrack("vide", vts, v_ref.stsd, vd, v_ref.width, v_ref.height, vc, vs)
    audio = OutTrack("soun", ats, a_ref.stsd, ad)
    return write_mp4([video, audio], chunks)


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


def _mp4a_mp3(rate: int, channels: int, bitrate: int) -> bytes:
    dcd = _descr(0x04, bytes([0x6B, 0x15]) + (0).to_bytes(3, "big") + struct.pack(">II", bitrate, bitrate))
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
    idr_ids = 0
    chunks: list[tuple[int, list[bytes]]] = []
    syncs: list[bool] = []
    per_second = rate / spf  # MP3 frames per second
    taken = 0
    for i in range(n_video):
        if i % IDR_EVERY == 0:
            sample = with4(_idr(i420, width, height, idr_ids % 2))
            idr_ids += 1
            syncs.append(True)
        else:
            sample = with4(_skip(width, height, i % IDR_EVERY))
            syncs.append(False)
        chunks.append((0, [sample]))
        upto = len(frames) if i == n_video - 1 else min(len(frames), round((i + 1) * per_second))
        chunks.append((1, frames[taken:upto]))
        taken = upto
    video = OutTrack("vide", 1000, _avc1(width, height, sps, pps), vd, width << 16, height << 16, None, syncs)
    audio = OutTrack("soun", rate, _mp4a_mp3(rate, ch, bitrate), [spf] * len(frames))
    return write_mp4([video, audio], chunks)
