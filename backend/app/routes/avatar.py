"""Avatars: pre-rendered book clips and lip-sync renders (clip and stream)."""
import asyncio
import base64
import hashlib
import re
import secrets
import time
from contextlib import AsyncExitStack
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import books, deps, faces, users, voices
from ..boson import BosonError
from ..config import settings
from ..mp3dur import mp3_seconds


router = APIRouter()


def _avatar_dir(book_id: str) -> Path:
    return settings.data_dir / "cache" / "avatar" / book_id


def _guard_avatar_book(book_id: str) -> None:
    """Book clips live in cache/avatar/<book id>, next to the shared lip-sync folder "talk". With PER_USER only real,
    visible books get that far, so no book id can reach another account's clips through a reserved folder name."""
    users.guard_book(book_id)
    if settings.per_user and (book_id in ("talk", "books") or books.load_book(book_id) is None):
        raise HTTPException(404, "Book not found")


@router.get("/api/avatars/{book_id}")
def list_avatars(book_id: str):
    """Chunk ids that have a pre-rendered avatar clip, so the reader only asks for clips that exist."""
    if not deps._SAFE_NAME.match(book_id):
        raise HTTPException(404, "Book not found")
    _guard_avatar_book(book_id)
    d = _avatar_dir(book_id)
    return sorted(p.stem for p in d.glob("*.mp4")) if d.is_dir() else []


_TTS_URL = re.compile(r"^/media/tts/([0-9a-f]{64})\.mp3$")
_avatar_locks: dict[str, list] = {}  # key -> [lock, holders + waiters]; dropped only when nobody uses it


async def _acquire_avatar_key(key: str) -> None:
    """Take the per-key render lock. The count covers waiters too, so the entry is never replaced under them."""
    entry = _avatar_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        await entry[0].acquire()
    except BaseException:
        _drop_avatar_ref(key, entry)
        raise


def _release_avatar_key(key: str) -> None:
    entry = _avatar_locks[key]
    entry[0].release()
    _drop_avatar_ref(key, entry)


def _drop_avatar_ref(key: str, entry: list) -> None:
    entry[1] -= 1
    if entry[1] <= 0 and _avatar_locks.get(key) is entry:
        del _avatar_locks[key]


class LipSyncRequest(BaseModel):
    audio_url: str = Field(max_length=120)
    voice: str = Field(max_length=120)
    face_id: str | None = Field(None, max_length=200)  # the face shown (#113); omitted: the voice's own linked face


def _lipsync_face(req: LipSyncRequest) -> Path:
    """The portrait to animate. With `face_id`: that face from the caller's own library (the same visibility as
    /api/faces: per-account under PER_USER, the shared library otherwise), 404 when it is not theirs or unknown.
    Without: the face bound to the voice, as before (400 when the voice has none)."""
    if req.face_id is not None:
        face = faces.image_path(req.face_id) if faces.FACE_ID.match(req.face_id) else None
        if face is None or not face.is_file():
            raise HTTPException(404, "Face not found")
        return face
    face = faces.voice_face_path(req.voice) if voices.is_known_voice(req.voice) else None
    if face is None:
        raise HTTPException(400, "Lip-sync needs audio from /api/speak and a voice that has a face.")
    return face


def _lipsync_inputs(req: LipSyncRequest) -> tuple[Path, Path, str]:
    """Validate a lip-sync request: (cached TTS file, face image, cache key). Raises 400/404."""
    m = _TTS_URL.match(req.audio_url)
    if not m:
        raise HTTPException(400, "Lip-sync needs audio from /api/speak and a voice that has a face.")
    face = _lipsync_face(req)
    audio = settings.data_dir / "cache" / "tts" / f"{m.group(1)}.mp3"
    if not audio.is_file():
        raise HTTPException(404, "That audio is not cached.")
    key = f"{m.group(1)}-{hashlib.sha256(face.read_bytes()).hexdigest()[:16]}"
    # the size is part of the key (a 3:4 clip is not a 1:1 clip); the default keeps its old key so cached clips stay valid
    return audio, face, key if settings.avatar_size == "640x640" else f"{key}-{settings.avatar_size}"


@router.post("/api/avatar/talk", dependencies=[Depends(deps.require_access)])
async def lipsync(req: LipSyncRequest, request: Request):
    """Lip-synced clip of one answer sentence: the voice's face speaking that cached TTS audio (~10 s to render).

    Only audio this server synthesised (/media/tts/<key>.mp3) and only voices with a face are accepted; clips are
    cached by audio key, so the same sentence never renders twice.
    """
    audio, face, key = _lipsync_inputs(req)
    clip = deps._lipsync_dir() / f"{key}.mp4"
    await _acquire_avatar_key(key)
    try:
        if not clip.is_file():
            if not deps.avatar_bucket.allow(deps.rate_key(request)):
                raise HTTPException(429, "Too many lip-sync renders; wait a moment.")
            data = audio.read_bytes()
            try:
                vid = await deps.boson.create_video(base64.b64encode(face.read_bytes()).decode(), base64.b64encode(data).decode(),
                                               image_mime=faces.FACE_EXTS[face.suffix.lstrip(".").lower()],
                                               seconds=mp3_seconds(data))
                await deps.boson.wait_video(vid, interval=1, timeout=120)
                video = await deps.boson.download_video(vid)
            except (BosonError, RuntimeError) as e:
                raise deps.boson_http_error(e)
            clip.parent.mkdir(parents=True, exist_ok=True)
            tmp = clip.with_name(f"{clip.stem}.{secrets.token_hex(6)}.tmp")
            tmp.write_bytes(video)
            tmp.replace(clip)
    finally:
        _release_avatar_key(key)
    return {"video_url": f"/media/avatar/{clip.name}"}


STREAM_MAX_BYTES = 40 * 1024 * 1024
STREAM_MAX_SECONDS = 150


@router.post("/api/avatar/stream", dependencies=[Depends(deps.require_access)])
async def lipsync_stream(req: LipSyncRequest, request: Request):
    """Streaming lip-sync: proxies Boson's fragmented-MP4 stream so playback starts at the first frames.

    A clip that is already cached answers with JSON {"video_url"} instead (the client plays that file). Otherwise the
    bytes are forwarded as they arrive and teed to a temp file that joins the cache only if the stream completes.
    """
    audio, face, key = _lipsync_inputs(req)
    clip = deps._lipsync_dir() / f"{key}.mp4"
    if clip.is_file():
        return {"video_url": f"/media/avatar/{clip.name}"}
    await _acquire_avatar_key(key)
    handed_off = False  # the response body owns the lock from the moment it is returned
    stack = AsyncExitStack()
    try:
        if clip.is_file():  # another request finished it while we waited
            return {"video_url": f"/media/avatar/{clip.name}"}
        if not deps.avatar_bucket.allow(deps.rate_key(request)):
            raise HTTPException(429, "Too many lip-sync renders; wait a moment.")
        data = audio.read_bytes()
        started = time.monotonic()
        try:
            async with asyncio.timeout(STREAM_MAX_SECONDS):
                _vid, chunks = await stack.enter_async_context(deps.boson.stream_video(
                    base64.b64encode(face.read_bytes()).decode(), base64.b64encode(data).decode(),
                    image_mime=faces.FACE_EXTS[face.suffix.lstrip(".").lower()], seconds=mp3_seconds(data)))
        except (BosonError, RuntimeError) as e:
            raise deps.boson_http_error(e)
        except TimeoutError:
            raise HTTPException(504, "Boson took too long to start the avatar stream.")

        async def body():
            tmp = clip.with_name(f"{clip.stem}.{secrets.token_hex(6)}.stream.tmp")  # unique per request
            ok = False
            total = 0
            try:
                clip.parent.mkdir(parents=True, exist_ok=True)
                it = chunks.__aiter__()
                with tmp.open("wb") as f:
                    while True:
                        remaining = started + STREAM_MAX_SECONDS - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("avatar stream exceeded its time budget")
                        try:
                            async with asyncio.timeout(remaining):  # a stalled upstream never yields: bound the wait
                                chunk = await it.__anext__()
                        except StopAsyncIteration:
                            break
                        total += len(chunk)
                        if total > STREAM_MAX_BYTES:
                            raise RuntimeError("avatar stream exceeded its size budget")
                        f.write(chunk)
                        yield chunk
                ok = total > 0
            finally:  # also runs on client disconnect (GeneratorExit / cancellation)
                try:
                    await stack.aclose()
                except BaseException:
                    ok = False
                if ok:
                    tmp.replace(clip)
                else:
                    tmp.unlink(missing_ok=True)
                _release_avatar_key(key)

        resp = StreamingResponse(body(), media_type="video/mp4", headers={"Cache-Control": "no-store"})
        handed_off = True
        return resp
    finally:
        if not handed_off:
            await stack.aclose()
            _release_avatar_key(key)


@router.get("/api/avatar/{chunk_id}")
def avatar(chunk_id: str, book: str):
    if not (deps._SAFE_NAME.match(chunk_id) and deps._SAFE_NAME.match(book)):
        raise HTTPException(404, "No avatar clip")
    _guard_avatar_book(book)
    path = _avatar_dir(book) / f"{chunk_id}.mp4"
    if not path.is_file():
        raise HTTPException(404, "No avatar clip for this line")
    return FileResponse(path, media_type="video/mp4")
