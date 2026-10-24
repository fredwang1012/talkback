"""Render the two muted loops shown for a voice's face: speaking (any answer, no wait) and idle (listening).

Usage: uv run --project backend python scripts/make_face_loops.py --name "Cloned narrator"
Thin wrapper over app.faces.animate (the same code behind POST /api/faces/<id>/animate). Cost: one short TTS line
plus two avatar renders (~20 s of video, a few cents). Reruns skip loops that already exist.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import faces, voices  # noqa: E402
from app.boson import BosonClient  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="registered voice name (data/voices.json)")
    args = ap.parse_args()
    voice = voices.load_voices().get(args.name)
    fid = next((f["id"] for f in faces.list_faces() if f["voice"] == voice), None) if voice else None
    if fid is None:
        sys.exit(f"{args.name!r} is not a registered voice with a face; run scripts/clone_voice.py --face first")
    settings.require_boson_key()
    print(await faces.animate(BosonClient(), fid))


if __name__ == "__main__":
    asyncio.run(main())
