"""#80: download a character's answer as one shareable MP4.

The app lip-syncs an answer sentence by sentence, so an answer is a list of cached clips (cache/avatar/talk, or the
caller's own lipsync folder with PER_USER). This joins them in order (app.mp4join, pure Python: the image has no
ffmpeg). An answer without a face becomes its cached TTS audio with a still picture the browser sends (the portrait or
letter circle, as raw I420 pixels).

Never renders: a sentence whose clip is not in the caller's cache is a 409 (the browser then offers the still version),
so a download can never spend Boson credit.
"""
import asyncio
import base64
import binascii
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import deps, mp4join, users
from ..mp3dur import mp3_seconds
from .avatar import LipSyncRequest, _TTS_URL, _lipsync_inputs
from ..config import settings

router = APIRouter()

MAX_SENTENCES = 40
MAX_INPUT_BYTES = 80 * 1024 * 1024  # all clips (or MP3s) of one answer together
MAX_SECONDS = 300  # a still clip's audio
STILL_SIDE = 320  # the still picture is STILL_SIDE x STILL_SIDE
STILL_BYTES = STILL_SIDE * STILL_SIDE * 3 // 2  # I420
_joins = asyncio.Semaphore(2)  # at most two clips read and joined at once; more are a 429, never a queue


class ClipSentence(BaseModel):
    audio_url: str = Field(max_length=120)
    voice: str = Field(max_length=120)
    face_id: str | None = Field(None, max_length=200)


class ClipRequest(BaseModel):
    book_id: str = Field(max_length=80)
    speaker: str = Field("", max_length=80)  # for the file name only
    sentences: list[ClipSentence] = Field(min_length=1, max_length=MAX_SENTENCES)
    # absent: join the lip-sync clips; present: base64 I420 STILL_SIDE x STILL_SIDE picture with the sentences' audio
    still: str | None = Field(None, max_length=(STILL_BYTES + 2) // 3 * 4 + 4)


def _filename(book_id: str, speaker: str) -> str:
    slug = lambda s: re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40]  # noqa: E731
    return f"talkback-{slug(book_id) or 'book'}-{slug(speaker) or 'answer'}.mp4"


def _read_all(paths) -> list[bytes]:
    """Runs in the worker thread, only while a `_joins` slot is held."""
    if sum(p.stat().st_size for p in paths) > MAX_INPUT_BYTES:
        raise HTTPException(413, "That answer is too long to download as one clip.")
    return [p.read_bytes() for p in paths]


def _still(pixels: bytes, paths) -> bytes:
    mp3s = _read_all(paths)
    if sum(mp3_seconds(b) for b in mp3s) > MAX_SECONDS:
        raise HTTPException(413, "That answer is too long to download as one clip.")
    return mp4join.still_mp4(pixels, STILL_SIDE, STILL_SIDE, mp3s)


def _faced(sentences: list[ClipSentence]) -> bytes:
    """The cached clips, joined. The caller's own cache only (lipsync_dir is per account with PER_USER; the face must be
    theirs too): resolving a face reads its image, so this runs in the worker thread under a slot too."""
    clips = []
    for s in sentences:
        _audio, _face, key = _lipsync_inputs(LipSyncRequest(audio_url=s.audio_url, voice=s.voice, face_id=s.face_id))
        clips.append(deps._lipsync_dir() / f"{key}.mp4")
    missing = sum(not c.is_file() for c in clips)
    if missing:
        raise HTTPException(409, f"{missing} of {len(clips)} sentence videos of this answer were never made.")
    return mp4join.join_clips(_read_all(clips))


@router.post("/api/clip/answer", dependencies=[Depends(deps.require_access)])
async def answer_clip(req: ClipRequest, request: Request):
    """One MP4 of a whole answer: its cached sentence clips joined in order, or (`still`) its audio over a still."""
    if not deps._SAFE_NAME.match(req.book_id):
        raise HTTPException(404, "Book not found")
    users.guard_book(req.book_id)
    if not deps.clip_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many clip downloads; wait a moment.")

    if req.still is not None:
        try:
            pixels = base64.b64decode(req.still, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "Bad picture.")
        if len(pixels) != STILL_BYTES:
            raise HTTPException(400, "Bad picture.")
        audio = []
        for s in req.sentences:
            m = _TTS_URL.match(s.audio_url)
            path = settings.data_dir / "cache" / "tts" / f"{m.group(1)}.mp3" if m else None
            if path is None or not path.is_file():
                raise HTTPException(404, "That audio is not cached.")
            audio.append(path)
        work = lambda: _still(pixels, audio)  # noqa: E731
    else:
        work = lambda: _faced(req.sentences)  # noqa: E731

    # every file read, decode and join happens in a worker thread under a slot; all slots busy: 429 at once
    if _joins.locked():
        raise HTTPException(429, "Busy making other clips; try again in a moment.")
    async with _joins:  # free (checked just above, no await in between): never waits
        try:
            mp4 = await asyncio.to_thread(work)
        except mp4join.Mp4Error as e:
            raise HTTPException(422, f"Could not join this answer's clips ({e}).")
    return Response(mp4, media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{_filename(req.book_id, req.speaker)}"',
        "Cache-Control": "no-store",
    })
