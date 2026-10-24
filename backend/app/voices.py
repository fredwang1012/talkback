"""Custom voice registration. Boson requires the user to own the right to clone the voice, so consent is mandatory."""
import io
import json
import wave

from . import users
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
    return users.voices_json()


def load_voices() -> dict[str, str]:
    p = _store()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_voice(name: str, voice_id: str) -> None:
    voices = load_voices()
    voices[name] = voice_id
    _store().parent.mkdir(parents=True, exist_ok=True)
    _store().write_text(json.dumps(voices, indent=1), encoding="utf-8")


def is_known_voice(voice: str) -> bool:
    """Presets or voices this server registered. Anything else would let a caller mint unlimited cache entries."""
    return voice in PRESET_VOICES or voice in load_voices().values()
