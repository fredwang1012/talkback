"""Clone a voice from a clean clip + transcript and give it a face (a portrait shown when a speaker uses the voice).

Usage: uv run --project backend python scripts/clone_voice.py --audio clip.wav --transcript clip.txt \
           --name "Cloned narrator" --face portrait.jpg --i-have-consent
Boson requires that you own the voice or have permission to clone it; so does this script (--i-have-consent).
The face is cropped to a centred square (top-weighted, where faces usually are) and stored as PNG. Writes
data/voices.json and data/voice_faces.json; both stay local (gitignored, never deployed).
Cost: one voice registration (small; logged as boson_voice_clone).
"""
import argparse
import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import faces, voices  # noqa: E402
from app.boson import BosonClient  # noqa: E402
from app.config import settings  # noqa: E402

FACE_SIZE = 640


def square_face(path: Path) -> bytes:
    """Centred square crop (a third of the spare height above the face) scaled to FACE_SIZE, as PNG bytes."""
    import pymupdf

    with pymupdf.open(path) as doc:
        page = doc[0]
        w, h = page.rect.width, page.rect.height
        side = min(w, h)
        x0, y0 = (w - side) / 2, (h - side) / 3
        clip = pymupdf.Rect(x0, y0, x0 + side, y0 + side)
        zoom = FACE_SIZE / side
        return page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--transcript", required=True, help="text file with exactly what is said in the clip")
    ap.add_argument("--name", required=True)
    ap.add_argument("--face")
    ap.add_argument("--i-have-consent", action="store_true")
    args = ap.parse_args()
    audio = Path(args.audio).read_bytes()
    transcript = Path(args.transcript).read_text(encoding="utf-8").strip()
    voices.validate_clone_request(audio, transcript, args.name, "true" if args.i_have_consent else "false")
    existing = voices.load_voices().get(args.name)
    if existing:
        voice_id = existing
        print(f"voice {args.name!r} already registered: {voice_id}")
    else:
        settings.require_boson_key()
        voice_id = await BosonClient().create_voice(base64.b64encode(audio).decode(), transcript, description=args.name)
        voices.save_voice(args.name, voice_id)
        print(f"cloned {args.name!r}: {voice_id}")
    if args.face:
        print("face:", faces.create_face(square_face(Path(args.face)), args.name, voice=voice_id)["image"])


if __name__ == "__main__":
    asyncio.run(main())
