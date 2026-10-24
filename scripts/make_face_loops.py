"""Render the two muted loops shown for a voice's face: speaking (any answer, no wait) and idle (listening).

Usage: uv run --project backend python scripts/make_face_loops.py --name "Cloned narrator"
The speaking loop is the face saying a neutral line in its own voice; the idle loop is the face over near-silence.
Both are written next to the portrait (data/portraits/voices/<voice>-talk.mp4 / -idle.mp4) and listed by
/api/voices/faces. Cost: one short TTS line plus two avatar renders (~20 s of video, a few cents). Reruns skip
loops that already exist.
"""
import argparse
import asyncio
import base64
import io
import math
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import voices  # noqa: E402
from app.boson import BosonClient  # noqa: E402
from app.config import settings  # noqa: E402

TALK_LINE = ("Now, let me tell you what happened next. It is a remarkable story, and I think you will enjoy it. "
             "Listen closely, because every detail matters.")
IDLE_SECONDS = 6.0


def near_silence(seconds: float, rate: int = 24000) -> bytes:
    """Very quiet room tone (a pure zero signal can be rejected as 'no audio')."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = (int(12 * math.sin(i * 0.05)) for i in range(int(seconds * rate)))
        w.writeframes(b"".join(struct.pack("<h", f) for f in frames))
    return buf.getvalue()


async def render(client: BosonClient, face: Path, audio: bytes, mime: str, seconds: float, out: Path) -> None:
    vid = await client.create_video(base64.b64encode(face.read_bytes()).decode(), base64.b64encode(audio).decode(),
                                    image_mime=voices.FACE_EXTS[face.suffix.lstrip(".").lower()], audio_mime=mime,
                                    seconds=seconds)
    await client.wait_video(vid, interval=2, timeout=300)
    out.write_bytes(await client.download_video(vid))
    print("wrote", out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="registered voice name (data/voices.json)")
    args = ap.parse_args()
    voice = voices.load_voices().get(args.name)
    face = voices.face_path(voice) if voice else None
    if face is None:
        sys.exit(f"{args.name!r} is not a registered voice with a face; run scripts/clone_voice.py --face first")
    settings.require_boson_key()
    client = BosonClient()
    talk, idle = voices.loop_path(voice, "talk"), voices.loop_path(voice, "idle")
    jobs = []
    if not talk.exists():
        speech = await client.create_speech(TALK_LINE, voice, response_format="mp3")
        jobs.append(render(client, face, speech.content, "audio/mpeg", len(TALK_LINE) / 15, talk))
    if not idle.exists():
        jobs.append(render(client, face, near_silence(IDLE_SECONDS), "audio/wav", IDLE_SECONDS, idle))
    await asyncio.gather(*jobs)
    print(voices.load_faces()[voice])


if __name__ == "__main__":
    asyncio.run(main())
