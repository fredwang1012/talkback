"""Boson TTS proxy with a disk cache, plus alignment of word timestamps to paragraph character offsets."""
import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path

from .boson import BosonClient
from .config import settings
from .textnorm import normalize_for_tts

MODEL = "higgs-tts-3"
FORMAT = "mp3"
_TAG = re.compile(r"<\|[^|]*\|>")
_locks: dict[str, asyncio.Lock] = {}


def cache_key(tts_text: str, voice: str, *, timestamps: bool = True, fmt: str = FORMAT, model: str = MODEL) -> str:
    """Stable across runs: any change to text, voice, format or flags yields a new audio file."""
    return hashlib.sha256(json.dumps([model, tts_text, voice, fmt, timestamps], ensure_ascii=False).encode()).hexdigest()


def _norm(word: str) -> str:
    return re.sub(r"[\W_]+", "", word.lower())


def _spoken_tokens(plain: str) -> list[tuple[str, int, int]]:
    """(normalized spoken piece, char_start, char_end) for each word of the paragraph text.
    A token such as '1888' expands to several spoken pieces that all map to the same character span."""
    out = []
    for m in re.finditer(r"[^\s—]+", plain):
        spoken = normalize_for_tts(m.group())
        pieces = [p for p in (_norm(x) for x in re.split(r"[\s-]+", spoken)) if p] or [_norm(m.group())]
        out += [(p, m.start(), m.end()) for p in pieces]
    return out


def align_timestamps(words: list[dict] | None, plain: str) -> list[dict] | None:
    """Map Boson word timestamps onto offsets in `plain` (the paragraph slice the chunk covers).

    Returns [{start, end, char_start, char_end}] (seconds, offsets into `plain`), or None when there is nothing to
    align (null/empty timestamps), in which case the frontend highlights the whole chunk."""
    if not words:
        return None
    tokens = _spoken_tokens(plain)
    if not tokens:
        return None
    result, cur = [], 0
    for w in words:
        text = w["word"]
        if _TAG.fullmatch(text.strip()) or not _norm(text):
            continue  # delivery tags and bare punctuation are not words
        target = _norm(_TAG.sub("", text))
        matched = None
        for j in range(cur, min(cur + 4, len(tokens))):
            joined = ""
            for n in range(j, min(j + 3, len(tokens))):  # a timestamp word may span several spoken pieces
                joined += tokens[n][0]
                if joined == target or (len(target) > 3 and (joined.startswith(target) or target.startswith(joined)) and len(joined) >= 3):
                    matched = (j, n)
                    break
            if matched:
                break
        if matched:
            j, n = matched
            cs, ce = tokens[j][1], tokens[n][2]
            cur = n + 1
        else:  # could not match: keep the previous token's span so highlighting never jumps backwards
            k = min(max(cur - 1, 0), len(tokens) - 1)
            cs, ce = tokens[k][1], tokens[k][2]
        result.append({"start": float(w["start"]), "end": float(w["end"]), "char_start": cs, "char_end": ce})
    return result or None


def _paths(key: str) -> tuple[Path, Path]:
    d = settings.data_dir / "cache" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.{FORMAT}", d / f"{key}.json"


def is_cached(tts_text: str, voice: str) -> bool:
    audio_path, meta_path = _paths(cache_key(tts_text, voice))
    return audio_path.exists() and meta_path.exists()


async def get_or_create(client: BosonClient, tts_text: str, voice: str, plain: str) -> dict:
    """Return {audio_url, timestamps, cached}. Concurrent requests for the same chunk share one Boson call."""
    key = cache_key(tts_text, voice)
    audio_path, meta_path = _paths(key)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if audio_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {"audio_url": f"/media/tts/{key}.{FORMAT}", "timestamps": meta["timestamps"], "cached": True}
        resp = await client.create_speech(tts_text, voice, timestamps=True, response_format=FORMAT)
        data = resp.json()
        audio_path.write_bytes(base64.b64decode(data["audio"]))  # the envelope holds base64 audio, not raw bytes
        aligned = align_timestamps(data.get("timestamps"), plain)
        meta_path.write_text(json.dumps({"timestamps": aligned, "raw": data.get("timestamps")}), encoding="utf-8")
    return {"audio_url": f"/media/tts/{key}.{FORMAT}", "timestamps": aligned, "cached": False}
