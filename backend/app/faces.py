"""Face library: portraits that any speaker can use, optionally animated into talk/idle loops.

Files live in data/portraits/faces/<id>.<ext> with <id>-talk.mp4 / <id>-idle.mp4 next to the portrait; metadata
in data/faces.json ({id: {name, file, voice?}}). A face created with a voice clone carries that voice id, so the
older voice -> face lookups (lip-sync, /api/voices/faces) are answered from the same list.
"""
import asyncio
from glob import escape as glob_escape
import base64
import io
import hashlib
import json
import logging
import math
import re
import secrets
import shutil
import struct
import threading
import wave
from pathlib import Path

from .boson import BosonClient
from . import users
from .config import settings

FACE_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
MAX_FACE_BYTES = 5 * 1024 * 1024
LOOPS = ("talk", "idle")
FACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

TALK_LINE = ("Now, let me tell you what happened next. It is a remarkable story, and I think you will enjoy it. "
             "Listen closely, because every detail matters.")
IDLE_SECONDS = 6.0
APPROX_ANIMATE_COST_USD = 0.05  # script notes: one short TTS line + two avatar renders (~20 s video), "a few cents"

_lock = threading.Lock()
log = logging.getLogger(__name__)


class FaceInputError(ValueError):
    pass


def _store() -> Path:
    return users.faces_json()


def face_dir() -> Path:
    return users.faces_dir()


def _legacy_dir() -> Path:
    return settings.data_dir / "portraits" / "voices"


def _read() -> dict[str, dict]:
    p = _store()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write(entries: dict[str, dict]) -> None:
    p = _store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    tmp.replace(p)


def sniff_ext(image: bytes) -> str | None:
    """Image type from magic bytes (the filename is client-supplied and proves nothing)."""
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "webp"
    return None


def validate_image(image: bytes) -> str:
    """Returns the extension, or raises FaceInputError."""
    if len(image) > MAX_FACE_BYTES:
        raise FaceInputError("The face image is larger than 5 MB.")
    ext = sniff_ext(image)
    if ext is None:
        raise FaceInputError("The face must be a PNG, JPEG or WebP image.")
    return ext


def new_id() -> str:
    return "f" + secrets.token_hex(8)


def _migrate_legacy() -> None:
    """Fold data/voice_faces.json (voice id -> file in portraits/voices/) into the library, once. Caller holds _lock.

    Never raises: a corrupt or unwritable legacy file is logged and skipped so faces and lip-sync keep working.
    Single instance (--max-instances 1), so no cross-process race; ids derive from the voice id, so a re-run
    overwrites instead of duplicating."""
    if settings.per_user:  # the legacy single-user file is handled by users.migrate_to_owner
        return
    try:
        _do_migrate_legacy()
    except Exception:
        log.exception("could not migrate voice_faces.json into the face library; skipping")


def _do_migrate_legacy() -> None:
    legacy = settings.data_dir / "voice_faces.json"
    if not legacy.exists():
        return
    from . import voices
    old = json.loads(legacy.read_text(encoding="utf-8"))
    entries = _read()
    have = {e.get("voice") for e in entries.values()}
    names = {v: n for n, v in voices.load_voices().items()}
    for voice, fname in old.items():
        src = _legacy_dir() / fname
        if voice in have or not src.is_file():
            continue
        fid = "v" + hashlib.sha256(voice.encode()).hexdigest()[:16]
        face_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, face_dir() / f"{fid}{src.suffix}")
        for kind in LOOPS:
            clip = src.with_name(f"{src.stem}-{kind}.mp4")
            if clip.is_file():
                shutil.copy2(clip, face_dir() / f"{fid}-{kind}.mp4")
        entries[fid] = {"name": names.get(voice, voice), "file": f"{fid}{src.suffix}", "voice": voice}
    _write(entries)
    legacy.replace(legacy.with_name("voice_faces.json.migrated"))


def _entries() -> dict[str, dict]:
    with _lock:
        _migrate_legacy()
        return {k: v for k, v in _read().items() if (face_dir() / v["file"]).is_file()}


def _view(fid: str, e: dict) -> dict:
    stem = Path(e["file"]).stem
    out = {"id": fid, "name": e["name"], "image": f"/portraits/faces/{e['file']}", "voice": e.get("voice")}
    for kind in LOOPS:
        out[kind] = f"/portraits/faces/{stem}-{kind}.mp4" if (face_dir() / f"{stem}-{kind}.mp4").is_file() else None
    return out


def list_faces() -> list[dict]:
    return [_view(k, v) for k, v in _entries().items()]


def get_face(fid: str) -> dict | None:
    if not FACE_ID.match(fid):
        return None
    e = _entries().get(fid)
    return _view(fid, e) if e else None


def face_exists(fid: str) -> bool:
    return get_face(fid) is not None


def image_path(fid: str) -> Path | None:
    e = _entries().get(fid) if FACE_ID.match(fid) else None
    return face_dir() / e["file"] if e else None


def loop_path(fid: str, kind: str) -> Path | None:
    img = image_path(fid)
    return img.with_name(f"{img.stem}-{kind}.mp4") if img else None


def _delete_files(e: dict) -> None:
    stem = Path(e["file"]).stem
    for f in [face_dir() / e["file"], *(face_dir() / f"{stem}-{k}.mp4" for k in LOOPS)]:
        f.unlink(missing_ok=True)


def create_face(image: bytes, name: str, voice: str | None = None) -> dict:
    """Store a portrait. With `voice`, replaces that voice's existing face (one face per cloned voice)."""
    ext = validate_image(image)
    name = name.strip()
    if not name:
        raise FaceInputError("Give the face a name.")
    with _lock:
        _migrate_legacy()
        entries = _read()
        if voice:
            for old in [k for k, v in entries.items() if v.get("voice") == voice]:
                _delete_files(entries.pop(old))
        fid = new_id()
        face_dir().mkdir(parents=True, exist_ok=True)
        (face_dir() / f"{fid}.{ext}").write_bytes(image)
        entries[fid] = {"name": name[:60], "file": f"{fid}.{ext}", **({"voice": voice} if voice else {})}
        _write(entries)
    return _view(fid, entries[fid])


def delete_legacy_voice_files(voice: str) -> None:
    """Remove the pre-library portrait of a voice (portraits/voices/<file>, -idle/-talk mp4s) when it is bound to this
    voice id by the old voice_faces.json (or named after it). Shared folder: left alone with PER_USER on."""
    if settings.per_user:
        return
    names: set[str] = set()
    for f in ("voice_faces.json", "voice_faces.json.migrated"):
        try:
            m = json.loads((settings.data_dir / f).read_text(encoding="utf-8"))
            if isinstance(m, dict) and isinstance(m.get(voice), str):
                names.add(m[voice])
        except (OSError, ValueError):
            pass
    d = _legacy_dir()
    for n in names:
        stem = Path(n).stem
        for f in (d / Path(n).name, d / f"{stem}-idle.mp4", d / f"{stem}-talk.mp4"):
            f.unlink(missing_ok=True)
    if d.is_dir():   # exact names only: "abc" must not take "abcdef.png" with it
        for f in [p for p in d.glob(f"{glob_escape(voice)}.*") if p.stem == voice]:
            f.unlink(missing_ok=True)
        for f in (d / f"{voice}-idle.mp4", d / f"{voice}-talk.mp4"):
            f.unlink(missing_ok=True)


def delete_face(fid: str) -> bool:
    if not FACE_ID.match(fid):
        return False
    with _lock:
        _migrate_legacy()
        entries = _read()
        e = entries.pop(fid, None)
        if e is None:
            return False
        _delete_files(e)
        _write(entries)
    return True


def face_ids_for_voice(voice: str) -> list[str]:
    return [k for k, v in _entries().items() if v.get("voice") == voice]


def link_voice(fid: str, voice: str) -> dict:
    """Bind an existing face of the caller's library to a cloned voice (#113): no copy, the face stays one entry.
    `linked` marks it as borrowed, so deleting the voice later drops the link and keeps the face. Returns the view;
    raises FaceInputError for an unknown face. Any other face bound to that voice loses the binding."""
    if not FACE_ID.match(fid):
        raise FaceInputError("Unknown face.")
    with _lock:
        _migrate_legacy()
        entries = _read()
        e = entries.get(fid)
        if e is None or not (face_dir() / e["file"]).is_file():
            raise FaceInputError("Unknown face.")
        for other in entries.values():
            if other is not e and other.get("voice") == voice:
                other.pop("voice", None)
                other.pop("linked", None)
        e["voice"] = voice
        e["linked"] = True
        _write(entries)
    return _view(fid, e)


def unlink_voice(voice: str) -> list[str]:
    """Drop the voice binding of every face borrowed by `voice` (link_voice); the faces stay. Returns their ids.
    Faces made with the clone itself (uploaded alongside it, no `linked` mark) are left for the caller to delete."""
    out: list[str] = []
    with _lock:
        _migrate_legacy()
        entries = _read()
        for k, v in entries.items():
            if v.get("voice") == voice and v.get("linked"):
                v.pop("voice", None)
                v.pop("linked", None)
                out.append(k)
        if out:
            _write(entries)
    return out


def own_face_ids_for_voice(voice: str) -> list[str]:
    """Faces that were created with this voice's clone (not borrowed from the library): these go with the voice."""
    return [k for k, v in _entries().items() if v.get("voice") == voice and not v.get("linked")]


# ---- voice-bound lookups (lip-sync and the older /api/voices/faces) ----

def voice_face_path(voice: str) -> Path | None:
    for k, v in _entries().items():
        if v.get("voice") == voice:
            return face_dir() / v["file"]
    return None


def voice_faces() -> dict[str, dict]:
    """voice id -> {"image", "talk", "idle"} for faces bound to a cloned voice."""
    return {v["voice"]: {k: v[k] for k in ("image", *LOOPS)} for v in list_faces() if v["voice"]}


# ---- animation (Boson Higgs Avatar) ----

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


async def _render(client: BosonClient, face: Path, audio: bytes, mime: str, seconds: float, out: Path) -> None:
    vid = await client.create_video(base64.b64encode(face.read_bytes()).decode(), base64.b64encode(audio).decode(),
                                    image_mime=FACE_EXTS[face.suffix.lstrip(".").lower()], audio_mime=mime,
                                    seconds=seconds)
    await client.wait_video(vid, interval=2, timeout=300)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(await client.download_video(vid))
    tmp.replace(out)


def needs_animation(fid: str) -> bool:
    return any(not loop_path(fid, k).exists() for k in LOOPS)


async def animate(client: BosonClient, fid: str) -> dict:
    """Render the missing talk/idle loops for a face (skips loops that exist). Returns the updated face view.

    The talk loop is the face saying a neutral line in its own voice (the bound voice, else the default preset);
    the idle loop is the face over near-silence. Cost: one short TTS line plus up to two ~10-20 s avatar renders.
    """
    img = image_path(fid)
    if img is None:
        raise FaceInputError("Unknown face.")
    face = get_face(fid)
    jobs = []
    if not loop_path(fid, "talk").exists():
        speech = await client.create_speech(TALK_LINE, face["voice"] or "default", response_format="mp3")
        jobs.append(_render(client, img, speech.content, "audio/mpeg", len(TALK_LINE) / 15, loop_path(fid, "talk")))
    if not loop_path(fid, "idle").exists():
        jobs.append(_render(client, img, near_silence(IDLE_SECONDS), "audio/wav", IDLE_SECONDS, loop_path(fid, "idle")))
    try:
        async with asyncio.TaskGroup() as tg:  # first failure cancels the sibling render: nothing runs unawaited
            for j in jobs:
                tg.create_task(j)
    except ExceptionGroup as eg:
        for k in LOOPS:
            loop_path(fid, k).with_suffix(".tmp").unlink(missing_ok=True)
        raise eg.exceptions[0] from None
    return get_face(fid)
