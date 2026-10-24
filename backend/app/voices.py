"""Custom voice registration. Boson requires the user to own the right to clone the voice, so consent is mandatory."""
import io
import json
import re
import wave
from pathlib import Path

from .config import settings

PRESET_VOICES = frozenset({"default", "chloe", "eleanor", "jake", "marcus", "nora", "oliver"})
MAX_BYTES = 10 * 1024 * 1024
MIN_SECONDS = 3.0


class VoiceInputError(ValueError):
    pass


def wav_seconds(audio: bytes) -> float | None:
    """Duration of a WAV clip, or None for other containers (those are validated by Boson itself)."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return None


def validate_clone_request(audio: bytes, transcript: str, name: str, consent: str) -> None:
    if consent.strip().lower() != "true":
        raise VoiceInputError("Consent is required: confirm this is your voice or you have permission to clone it.")
    if not transcript.strip():
        raise VoiceInputError("A transcript of the recording is required.")
    if not name.strip():
        raise VoiceInputError("Give the voice a name.")
    if len(audio) > MAX_BYTES:
        raise VoiceInputError("The recording is larger than 10 MB.")
    seconds = wav_seconds(audio)
    if seconds is not None and seconds < MIN_SECONDS:
        raise VoiceInputError(f"The recording is {seconds:.1f} s long; it must be at least {MIN_SECONDS:.0f} s.")


def _store():
    return settings.data_dir / "voices.json"


def load_voices() -> dict[str, str]:
    p = _store()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_voice(name: str, voice_id: str) -> None:
    voices = load_voices()
    voices[name] = voice_id
    _store().write_text(json.dumps(voices, indent=1), encoding="utf-8")


def is_known_voice(voice: str) -> bool:
    """Presets or voices this server registered. Anything else would let a caller mint unlimited cache entries."""
    return voice in PRESET_VOICES or voice in load_voices().values()


# ---------- faces: an optional portrait per cloned voice, shown whenever a speaker uses that voice ----------

FACE_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
MAX_FACE_BYTES = 5 * 1024 * 1024


def _faces_store():
    return settings.data_dir / "voice_faces.json"


def _face_dir():
    return settings.data_dir / "portraits" / "voices"


LOOPS = ("talk", "idle")  # optional muted clips next to the portrait: <stem>-talk.mp4 (speaking), <stem>-idle.mp4


def loop_path(voice_id: str, kind: str) -> Path | None:
    """Where the `kind` loop for a voice with a face lives (None if the voice has no face)."""
    face = face_path(voice_id)
    return face.with_name(f"{face.stem}-{kind}.mp4") if face else None


def load_faces() -> dict[str, dict]:
    """voice id -> {"image": URL, "talk": URL|None, "idle": URL|None}, all served from /portraits/voices/."""
    p = _faces_store()
    files = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    out = {}
    for voice, name in files.items():
        if not (_face_dir() / name).is_file():
            continue
        entry = {"image": f"/portraits/voices/{name}"}
        for kind in LOOPS:
            clip = _face_dir() / f"{Path(name).stem}-{kind}.mp4"
            entry[kind] = f"/portraits/voices/{clip.name}" if clip.is_file() else None
        out[voice] = entry
    return out


def face_path(voice_id: str) -> Path | None:
    p = _faces_store()
    name = (json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}).get(voice_id)
    return _face_dir() / name if name and (_face_dir() / name).is_file() else None


def save_face(voice_id: str, image: bytes, ext: str) -> str:
    """Store the portrait for a registered voice; returns its URL. Raises VoiceInputError on bad input."""
    ext = ext.lower().lstrip(".")
    if ext not in FACE_EXTS:
        raise VoiceInputError("The face must be a PNG, JPEG or WebP image.")
    if len(image) > MAX_FACE_BYTES:
        raise VoiceInputError("The face image is larger than 5 MB.")
    if voice_id not in load_voices().values():
        raise VoiceInputError("Unknown voice.")
    name = re.sub(r"[^A-Za-z0-9_-]", "_", voice_id)[:80] + f".{ext}"
    _face_dir().mkdir(parents=True, exist_ok=True)
    (_face_dir() / name).write_bytes(image)
    p = _faces_store()
    files = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    files[voice_id] = name
    p.write_text(json.dumps(files, indent=1), encoding="utf-8")
    return f"/portraits/voices/{name}"
