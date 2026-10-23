from pathlib import Path

import pytest

from app.mp3dur import mp3_seconds

# 32 kbit/s, 24 kHz, mono MPEG-2 L3 frame: header FF F3 18 C4, 576 samples -> 96 bytes, 24 ms
FRAME = bytes([0xFF, 0xF3, 0x18, 0xC4]) + b"\x00" * 92


def test_cbr_frames_32kbps():
    assert mp3_seconds(FRAME * 250) == pytest.approx(6.0, rel=0.01)


def test_skips_id3v2_tag():
    tag = b"ID3\x04\x00\x00" + bytes([0, 0, 0x02, 0x00]) + b"\x00" * 256
    assert mp3_seconds(tag + FRAME * 250) == pytest.approx(6.0, rel=0.01)


def test_not_mp3_falls_back_to_4000_bytes_per_second():
    assert mp3_seconds(b"\x00" * 8000) == 2.0
    assert mp3_seconds(b"") == 0.0


def test_real_cached_clip_is_four_times_old_estimate():
    f = next(Path("C:/dev/UofT/Hackathons/talkback/data/cache/tts").glob("*.mp3"), None)
    if f is None:
        pytest.skip("no cached tts")
    d = f.read_bytes()
    assert mp3_seconds(d) == pytest.approx(len(d) / 4000, rel=0.1)
