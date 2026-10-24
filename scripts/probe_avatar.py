"""Probe (M9): does /v1/videos accept our request shape, and does the MP4 contain an audio track?

Needs BOSON_API_KEY, one portrait at data/portraits/<book>/<speaker>.png|jpg|webp, and one cached TTS clip.
Usage: uv run --project backend python scripts/probe_avatar.py --portrait path/to/portrait.png
Cost: one short clip, a few cents. Records the findings in docs/internal/NOTES.md.
"""
import argparse
import asyncio
import base64
import re

from _common import BosonClient, BosonError, CAPTURES, flush_notes, note

SENTENCE = "Good evening. I have been expecting you."


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portrait", required=True)
    args = ap.parse_args()
    from pathlib import Path

    client = BosonClient()
    img = Path(args.portrait)
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[img.suffix.lstrip(".").lower()]
    speech = await client.create_speech(SENTENCE, "oliver", response_format="mp3")
    audio = base64.b64encode(speech.content).decode() if speech.headers.get("content-type", "").startswith("audio") else speech.json()["audio"]
    try:
        vid = await client.create_video(base64.b64encode(img.read_bytes()).decode(), audio, image_mime=mime)
        info = await client.wait_video(vid)
    except BosonError as e:
        note(f"create_video failed with data-URI inputs: {e}. Adjust Boson.create_video (maybe raw base64 or multipart).")
        flush_notes("avatar (M9)")
        return
    data = await client.download_video(vid)
    out = CAPTURES / "avatar_probe.mp4"
    out.write_bytes(data)
    has_audio = bool(re.search(rb"mp4a|soun", data[:4000]))  # crude: an audio track shows up as a 'soun' handler / 'mp4a' box
    note(f"video {vid} completed; keys={sorted(info)}; {len(data)} bytes saved to {out.name}")
    note(f"MP4 appears to {'contain' if has_audio else 'NOT contain'} an audio track (verify by playing it).")
    note("If it has audio: play the MP4 instead of the MP3 for that chunk. If not: keep the video muted and play both (the Stage already mutes it).")
    flush_notes("avatar (M9)")


asyncio.run(main())
