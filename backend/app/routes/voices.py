"""Voice cloning, previews, account voices and the voice list."""
import asyncio
import base64
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import deps, faces, users, voices
from ..boson import BosonError


router = APIRouter()


@router.post("/api/voices/clone", dependencies=[Depends(deps.require_access)])
async def clone_voice(request: Request, audio: UploadFile = File(...), transcript: str = Form(..., max_length=2000),
                      name: str = Form(..., max_length=60), consent: str = Form("false"),
                      face: UploadFile | None = File(None), face_id: str | None = Form(None, max_length=80)):
    """Clone a voice. `face` (an image) stores a new face bound to it; `face_id` (#113) links an existing face of the
    caller's library instead, with no copy: deleting the voice later only drops that link."""
    if not deps.clone_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many voice uploads; wait a moment.")
    data = await audio.read(voices.MAX_BYTES + 1)  # bounded read: never buffer an unbounded upload
    image = await face.read(faces.MAX_FACE_BYTES + 1) if face is not None else None
    if face_id is not None and (not faces.FACE_ID.match(face_id) or not faces.face_exists(face_id)):
        raise HTTPException(404, "Face not found")  # before the paid clone call
    try:
        voices.validate_clone_request(data, transcript, name, consent)
        if image is not None:
            faces.validate_image(image)  # before the paid clone call
    except (voices.VoiceInputError, faces.FaceInputError) as e:
        raise HTTPException(400, str(e))
    try:
        voice_id = await deps.boson.create_voice(base64.b64encode(data).decode(), transcript, description=name)
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    voices.save_voice(name, voice_id)
    face_url = None
    if image is not None:
        face_url = faces.create_face(image, name, voice=voice_id)["image"]
    elif face_id is not None:
        try:
            face_url = faces.link_voice(face_id, voice_id)["image"]
        except faces.FaceInputError:   # deleted between the check and the clone: the voice stays, without a face
            face_url = None
    return {"voice": voice_id, "name": name, "face": face_url}


_VOICE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


@router.post("/api/voices/preview", dependencies=[Depends(deps.require_access)])
async def preview_voice(request: Request, audio: UploadFile = File(...), transcript: str = Form(..., max_length=2000),
                        consent: str = Form("false")):
    """Hear a recording as a voice before saving it: one-off cloning (ref_audio), nothing is stored at Boson."""
    if not deps.clone_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many voice previews; wait a moment.")
    data = await audio.read(voices.MAX_BYTES + 1)
    try:
        voices.validate_clone_request(data, transcript, "preview", consent)   # the same consent as cloning
    except voices.VoiceInputError as e:
        raise HTTPException(400, str(e))
    try:
        mp3 = await deps.boson.preview_voice(base64.b64encode(data).decode(), transcript, voices.PREVIEW_TEXT)
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    return Response(content=mp3, media_type="audio/mpeg")


@router.get("/api/voices/account", dependencies=[Depends(deps.require_access)])
async def account_voices():
    """Custom voices on the Boson account that this library has not saved yet (owner or local run only)."""
    if not voices.can_see_account_voices():
        raise HTTPException(404, "Not found")
    try:
        found = await deps.boson.list_voices()
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    saved = set(voices.load_voices().values())
    return [v for v in found if v["id"] not in saved]


class ImportVoiceRequest(BaseModel):
    voice_id: str = Field(..., max_length=200)
    name: str = Field(..., min_length=1, max_length=60)


@router.post("/api/voices/import", dependencies=[Depends(deps.require_access)])
async def import_voice(req: ImportVoiceRequest):
    """Save a voice that already exists on the Boson account (made with another app or an earlier install)."""
    if not voices.can_see_account_voices():
        raise HTTPException(404, "Not found")
    if not _VOICE_ID.match(req.voice_id):
        raise HTTPException(400, "Bad voice id")
    try:
        found = await deps.boson.list_voices()
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    if req.voice_id not in {v["id"] for v in found}:
        raise HTTPException(404, "That voice is not on this Boson account.")
    voices.save_voice(req.name.strip(), req.voice_id)
    return {"voice": req.voice_id, "name": req.name.strip()}


@router.delete("/api/voices/{voice_id}", dependencies=[Depends(deps.require_access)])
async def delete_voice(voice_id: str):
    """Delete a voice the caller cloned: at Boson and from the saved list. Its face stays in the library, only the link
    goes (deleting a face likewise keeps the voice). Casts that used the voice fall back to Auto. Built-in presets are
    refused."""
    users.require_account()
    if voice_id in voices.PRESET_VOICES:
        raise HTTPException(403, "Built-in voices cannot be deleted.")
    if not _VOICE_ID.match(voice_id) or not voices.is_cloned_voice(voice_id):   # this account's own list only
        raise HTTPException(404, "Voice not found")
    try:
        await deps.boson.delete_voice(voice_id)   # False (404) = already gone at Boson: carry on
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)            # local data stays, so the delete can be retried
    face_ids = await asyncio.to_thread(faces.unlink_voice, voice_id)   # faces stay; they only lose the voice link
    await asyncio.to_thread(faces.delete_legacy_voice_files, voice_id)
    voices.remove_voice(voice_id)
    return {"deleted": voice_id, "faces": face_ids}


@router.get("/api/voices", dependencies=[Depends(deps.require_access)])
def list_voices():
    return voices.load_voices()


@router.get("/api/voices/faces", dependencies=[Depends(deps.require_access)])
def list_voice_faces():
    """voice id -> portrait URL for cloned voices that have a face (shown for any speaker using that voice).
    Gated: the face is a real person's. The files are named after the voice id, which only these gated lists reveal."""
    return faces.voice_faces()
