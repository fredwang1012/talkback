"""Estimate an MP3's duration from its own frame headers (no dependencies)."""

FALLBACK_BYTES_PER_SECOND = 4000  # our TTS mp3s are 32 kbit/s

_BITRATES = {  # kbit/s, Layer III; index 1..14
    1: [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


def _first_frame(data: bytes) -> tuple[int, int, int, int, int] | None:
    """(offset, bitrate_bps, sample_rate, samples_per_frame, channels) of the first valid Layer III frame."""
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        pos = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    for i in range(pos, min(len(data) - 4, pos + 65536)):
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            continue
        b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
        vbits, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        br_i, sr_i = b2 >> 4, (b2 >> 2) & 3
        if vbits == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
            continue
        ver = {3: 1, 2: 2, 0: 25}[vbits]
        bitrate = _BITRATES[1 if ver == 1 else 2][br_i - 1] * 1000
        return i, bitrate, _RATES[ver][sr_i], 1152 if ver == 1 else 576, 1 if (b3 >> 6) == 3 else 2
    return None


def mp3_seconds(data: bytes) -> float:
    """Duration in seconds: Xing/Info frame count if present, else bytes / first-frame bitrate, else len/4000."""
    hit = _first_frame(data)
    if hit is None:
        return len(data) / FALLBACK_BYTES_PER_SECOND
    off, bitrate, rate, spf, ch = hit
    side = (17 if ch == 1 else 32) if spf == 576 else (9 if ch == 1 else 17)
    x = off + 4 + side
    if data[x + 4 : x + 8] in (b"Xing", b"Info") or data[x : x + 4] in (b"Xing", b"Info"):
        x = x if data[x : x + 4] in (b"Xing", b"Info") else x + 4
        if data[x + 7] & 1 and len(data) >= x + 12:
            frames = int.from_bytes(data[x + 8 : x + 12], "big")
            if frames:
                return frames * spf / rate
    return (len(data) - off) * 8 / bitrate
