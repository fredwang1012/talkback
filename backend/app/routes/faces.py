"""The face library: upload, detect, list, delete, animate."""
import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from .. import deps, face_detect, faces, users
from ..boson import BosonError


router = APIRouter()


_face_locks: dict[str, asyncio.Lock] = {}


def _safe_face_id(face_id: str) -> str:
    if not faces.FACE_ID.match(face_id):
        raise HTTPException(404, "Face not found")
    return face_id


@router.post("/api/faces", dependencies=[Depends(deps.require_access)])
async def upload_face(request: Request, image: UploadFile = File(...), name: str = Form(..., max_length=60)):
    if not deps.face_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many face uploads; wait a moment.")
    data = await image.read(faces.MAX_FACE_BYTES + 1)  # bounded read
    try:
        return faces.create_face(data, name)
    except faces.FaceInputError as e:
        raise HTTPException(400, str(e))


@router.post("/api/faces/detect", dependencies=[Depends(deps.require_access)])
async def detect_face(request: Request, image: UploadFile = File(...)):
    """The main face's box ({x, y, w, h} as fractions, or null) for the head-and-shoulders crop (#109). Nothing is
    stored. Guarded like /api/books/describe: accounts only under PER_USER, a per-caller bucket here, and a global
    bucket, daily cap and budget reservation in face_detect."""
    users.require_account("Sign in to add faces.")
    if not deps.face_detect_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many photos at once; place the square yourself.")
    data = await image.read(faces.MAX_FACE_BYTES + 1)  # bounded read
    try:
        ext = faces.validate_image(data)
    except faces.FaceInputError as e:
        raise HTTPException(400, str(e))
    return {"face": await face_detect.detect(data, ext)}


@router.get("/api/faces", dependencies=[Depends(deps.require_access)])
def list_faces():
    """Gated: faces can be real people's portraits."""
    return faces.list_faces()


async def _delete_face_locked(face_id: str) -> bool:
    lock = _face_locks.setdefault(face_id, asyncio.Lock())
    try:
        async with lock:  # waits for a running animate, so no loops are written after the delete
            return faces.delete_face(face_id)
    finally:
        _release_face_lock(face_id, lock)


@router.delete("/api/faces/{face_id}", dependencies=[Depends(deps.require_access)])
async def delete_face(face_id: str):
    users.require_account()
    _safe_face_id(face_id)
    if not await _delete_face_locked(face_id):
        raise HTTPException(404, "Face not found")
    return {"deleted": face_id}


def _release_face_lock(face_id: str, lock: asyncio.Lock) -> None:
    if not lock.locked() and _face_locks.get(face_id) is lock:
        del _face_locks[face_id]


@router.post("/api/faces/{face_id}/animate", dependencies=[Depends(deps.require_access)])
async def animate_face(face_id: str, request: Request):
    """Generate the talk and idle loops (a few cents of Boson avatar credit). Idempotent: existing loops are kept."""
    _safe_face_id(face_id)
    if not faces.face_exists(face_id):
        raise HTTPException(404, "Face not found")
    lock = _face_locks.setdefault(face_id, asyncio.Lock())
    try:
        async with lock:  # a double click waits, then finds the loops and spends nothing
            if not faces.face_exists(face_id):  # deleted while we waited
                raise HTTPException(404, "Face not found")
            if faces.needs_animation(face_id):
                if not deps.avatar_bucket.allow(deps.rate_key(request)):
                    raise HTTPException(429, "Too many avatar renders; wait a moment.")
                try:
                    face = await faces.animate(deps.boson, face_id)
                except (BosonError, RuntimeError) as e:
                    raise deps.boson_http_error(e)
                return {**face, "animated": True, "approx_cost_usd": faces.APPROX_ANIMATE_COST_USD}
            return {**faces.get_face(face_id), "animated": False, "approx_cost_usd": 0}
    finally:
        _release_face_lock(face_id, lock)
