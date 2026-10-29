"""Bilingual reading (B3): a paragraph's translation, piece by piece, for reading it in a second language in the same
voice.

A paragraph is translated as the list of its TTS chunks' texts (one translated piece per chunk), so every piece keeps
the speaker, voice and delivery tags of the chunk it translates. Translation is lazy: only a paragraph the reader is
about to hear (or see) is translated, never the whole book, so the spoiler rule holds by construction. Each paragraph's
translation is cached on disk per book + paragraph + language (under the account's own folder with PER_USER), and
invalidated when the paragraph's chunk texts change. Gemini (TRANSLATE_MODEL, Flash-Lite by default) on
Vertex or AI Studio (app/gemini.py) when set up, else the public processing service on a clone (public_client.translate_passage)."""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel

from . import pacer as pacing
from . import gemini, tts_lang, users
from .config import settings
from .delivery import apply_speed
from .schema import Book, Paragraph
from .ttschunks import split_translation

Translator = Callable[[list[str], str], Awaitable[list[str]]]  # (pieces, language name) -> translated pieces

UNTRANSLATED_KINDS = ("code", "math")   # read as a short English note / an equation reading: not translated
MAX_PIECES = 40                         # chunks per paragraph sent in one call (a longer paragraph is sent in parts)
_TAG = re.compile(r"<\|([^|]*)\|>")
_locks: dict[str, asyncio.Lock] = {}


class Unavailable(Exception):
    """No translation backend: no GCP_PROJECT and no public processing service."""


class Pieces(BaseModel):
    pieces: list[str]


def system_prompt(language: str) -> str:
    return (f"You translate passages of an English book into {language} for a reader who listens to each paragraph in "
            f"English and then in {language}. You get a JSON list of consecutive pieces of one paragraph. Reply as JSON "
            f'{{"pieces": [...]}} with the same number of pieces, in the same order, each the {language} translation '
            "of the matching piece. Translate faithfully and naturally. Keep the names of people, places and things "
            "exactly as the book spells them (do not translate or transliterate names). Keep quotation marks around "
            "dialogue. No notes, no explanations, no romanization.")


def language_name(code: str) -> str:
    return tts_lang.LANGUAGES.get("zho" if code == "cmn" else code, code)


def code_for(name: str) -> str | None:
    return next((c for c, n in tts_lang.LANGUAGES.items() if n == name), None)


def cache_path(book_id: str, lang: str, para: int, ident: "users.Identity | None" = None) -> Path:
    """data/cache/translations/<book>/<lang>/p<idx>.json, or the same under data/users/<ns>/ with PER_USER."""
    return users.user_root(ident) / "cache" / "translations" / book_id / lang / f"p{para}.json"


def source_pieces(p: Paragraph) -> list[str]:
    """The text each TTS chunk of the paragraph shows (what is translated, one piece per chunk)."""
    return [p.text[c.char_start:c.char_end].strip() or c.tts_text for c in p.tts_chunks]


def _digest(pieces: list[str]) -> str:
    return hashlib.sha256(json.dumps(pieces, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------- backends ----------

class _InteractiveGemini:
    """GeminiProvider on the interactive lane of the shared pacer (the reader is waiting for the next paragraph)."""

    _llm = None

    @classmethod
    def llm(cls):
        if cls._llm is None:
            from .enrich.llm import LLM, GeminiProvider

            class Provider(GeminiProvider):
                priority: pacing.Priority = "interactive"
                RETRIES = pacing.INTERACTIVE["attempts"]

            cls._llm = LLM("gemini", {"gemini": Provider(model=settings.translate_model or None)}, fallback=False,
                           concurrency=8)
        return cls._llm


async def gemini_translate(pieces: list[str], language: str) -> list[str]:
    out = await _InteractiveGemini.llm().complete_json(system_prompt(language), json.dumps(pieces, ensure_ascii=False),
                                                      Pieces, "fast", max_tokens=200 + 3 * sum(map(len, pieces)))
    return out.pieces


async def _public_translate(pieces: list[str], language: str) -> list[str]:
    from . import public_client
    return await public_client.translate_passage(pieces, code_for(language) or language)


def default_translator() -> Translator:
    """Gemini (Vertex or AI Studio), else the public service on a clone; raises Unavailable otherwise."""
    if gemini.available():
        return gemini_translate
    from . import public_client
    if public_client.active():
        return _public_translate
    raise Unavailable("Translation needs Gemini (GCP_PROJECT or GEMINI_API_KEY) or the public processing service.")


async def _translate(src: list[str], language: str, translator: Translator) -> list[str]:
    """One translated piece per source piece: the whole list in one call (in parts past MAX_PIECES); a misaligned
    answer is redone one piece per call. A blank piece keeps the original text, so nothing is ever silent."""
    out: list[str] = []
    for i in range(0, len(src), MAX_PIECES):
        part = src[i:i + MAX_PIECES]
        got = await translator(part, language)
        if len(got) != len(part):
            got = [(r[0] if len(r) == 1 else " ".join(r)) for r in
                   await asyncio.gather(*(translator([x], language) for x in part))]
        out += got
    return [t.strip() if t and t.strip() else s for t, s in zip(out, src)]


async def paragraph_pieces(book: Book, p: Paragraph, lang: str, translator: Translator | None = None) -> tuple[list[str], bool]:
    """(translated pieces, one per TTS chunk; whether they came from the cache). Concurrent calls share one
    translation. Raises Unavailable when there is no backend."""
    src = source_pieces(p)
    digest = _digest(src)
    path = cache_path(book.id, lang, p.idx)
    lock = _locks.setdefault(str(path), asyncio.Lock())
    async with lock:
        cached = _read(path, digest)
        if cached is not None:
            return cached, True
        pieces = await _translate(src, language_name(lang), translator or default_translator())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"source": digest, "pieces": pieces}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return pieces, False


def _read(path: Path, digest: str) -> list[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pieces = data.get("pieces") if isinstance(data, dict) else None
    if data.get("source") != digest or not isinstance(pieces, list):
        return None
    return [str(x) for x in pieces]


# ---------- TTS input ----------

def lead_tags(tts_text: str) -> str:
    """The delivery tags at the start of a chunk's TTS text (voice pitch/speed, style, emotion, expressiveness,
    pause), without sound effects: an sfx belongs to English onomatopoeia the translation does not have."""
    tags, pos = [], 0
    while (m := _TAG.match(tts_text, pos)) is not None:
        if not m.group(1).startswith("sfx:"):
            tags.append(m.group(0))
        pos = m.end()
    return "".join(tags)


def tts_inputs(chunk_tts_text: str, piece: str, lang: str, speed: str | None = None) -> list[str]:
    """Boson inputs for one translated piece: cut to <= MAX_CHARS of text (sentence-aligned, CJK-aware), each with
    the original chunk's delivery tags so emotion and style carry over. The text is not English-normalized: Boson
    normalizes it (normalize=True) in its own language."""
    tags = lead_tags(chunk_tts_text)
    return [apply_speed(tags + part, speed) for part in split_translation(piece, lang)]
