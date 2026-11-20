"""Reading aloud: chunk TTS and bilingual reading (B3).
Logs as "app.main" (these lines were logged from main.py before the routers split, so they read the same)."""
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import booklang, books, cast, deps, translate, tts_cache, tts_lang, users, voices
from ..delivery import apply_speed
from ..boson import BosonError
from ..schema import Book
from ..ttschunks import inline_spans


router = APIRouter()


class TtsRequest(BaseModel):
    book_id: str
    chunk_id: str
    voice_override: str | None = None
    cached_only: bool = False  # demo mode: serve pre-rendered audio only, never call Boson
    speed: Literal["very_slow", "slow", "fast", "very_fast"] | None = None  # natural speed (#78): Boson speed tag, own cache entry


def _chunk_voice_and_text(book: Book, c, voice_override: str | None) -> tuple[str, str]:
    """The voice a chunk is read in (override, else the saved cast, else the book's) and its TTS text with the cast's
    pitch/speed tags in place of the book's. 400 for an unknown voice."""
    voice = voice_override or cast.voice_for(book, c.speaker) or c.voice
    if not voices.is_known_voice(voice):
        raise HTTPException(400, f"Unknown voice '{voice}'.")
    tts_text = c.tts_text
    ch = next((x for x in book.characters if x.id == c.speaker), None)
    tags = cast.tags_for(book, c.speaker) if ch else None
    if tags is not None and tts_text.startswith(ch.voice_tags):  # the user's pitch/speed replaces the book's
        tts_text = tags + tts_text[len(ch.voice_tags):]
    return voice, tts_text


@router.post("/api/tts", dependencies=[Depends(deps.require_access)])
async def tts(req: TtsRequest, request: Request):
    users.guard_book(req.book_id)
    book = books.load_book(req.book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    for p in book.paragraphs:
        for c in p.tts_chunks:
            if c.id == req.chunk_id:
                voice, tts_text = _chunk_voice_and_text(book, c, req.voice_override)
                tts_text = apply_speed(tts_text, req.speed)
                # a book in another language (#117): Boson gets its `language`, without word timestamps (the reader
                # then lights the whole chunk); English sends exactly what it always did
                lang = booklang.tts_language(book.language)
                extra = {"language": lang} if lang else {}
                cached = tts_cache.is_cached(tts_text, voice, **extra)
                if req.cached_only and not cached:
                    raise HTTPException(409, "This line is not pre-rendered yet. Run preprocess/prerender_tts.py before the demo.")
                if not cached and not deps.tts_bucket.allow(deps.rate_key(request)):
                    raise HTTPException(429, "Too many new audio requests; wait a moment.")
                try:
                    out = await tts_cache.get_or_create(deps.boson, tts_text, voice, p.text[c.char_start:c.char_end],
                                                        inline_spans(p, c.char_start, c.char_end), **extra)
                except (BosonError, RuntimeError) as e:
                    raise deps.boson_http_error(e)
                return {"chunk_id": c.id, "voice": voice, **out}
    raise HTTPException(404, "Chunk not found")


# ---------- bilingual reading (B3): a paragraph in a second language, same voice ----------


def _foreign_language(v: str) -> str:
    if not tts_lang.valid(v) or tts_lang.is_english(v):
        raise ValueError("language must be the ISO 639-3 code of a language other than English that Higgs TTS supports")
    return v


class TranslateRequest(BaseModel):
    book_id: str
    para: int = Field(ge=0)
    language: str = Field(max_length=3)

    _lang = field_validator("language")(classmethod(lambda cls, v: _foreign_language(v)))


class TranslatedTtsRequest(BaseModel):
    book_id: str
    chunk_id: str
    language: str = Field(max_length=3)
    voice_override: str | None = None
    cached_only: bool = False
    speed: Literal["very_slow", "slow", "fast", "very_fast"] | None = None

    _lang = field_validator("language")(classmethod(lambda cls, v: _foreign_language(v)))


def _translatable_book(book_id: str) -> Book:
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    return book


async def _pieces(book: Book, p, lang: str, request: Request) -> list[str]:
    """The paragraph's translation (cached, else one Gemini call, rate-limited per reader)."""
    if p.kind in translate.UNTRANSLATED_KINDS or not p.tts_chunks:
        raise HTTPException(400, "This paragraph is not translated (code or equation).")
    path = translate.cache_path(book.id, lang, p.idx)
    if not path.exists() and not deps.translate_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many new translations; wait a moment.")
    try:
        pieces, _ = await translate.paragraph_pieces(book, p, lang)
    except translate.Unavailable as e:
        raise HTTPException(503, str(e))
    except HTTPException:
        raise
    except Exception as e:
        logging.getLogger("app.main").warning("translation failed: %s", type(e).__name__)
        raise HTTPException(502, "The translation failed; try again.")
    return pieces


@router.post("/api/translate", dependencies=[Depends(deps.require_access)])
async def translate_paragraph(req: TranslateRequest, request: Request):
    """One paragraph in the reader's second language: a translated piece per TTS chunk (lazy, cached per book +
    paragraph + language). Only paragraphs the reader is about to read are asked for, never the whole book."""
    book = _translatable_book(req.book_id)
    if req.para >= len(book.paragraphs):
        raise HTTPException(404, "Paragraph not found")
    p = book.paragraphs[req.para]
    pieces = await _pieces(book, p, req.language, request)
    return {"para": p.idx, "language": req.language, "chunk_ids": [c.id for c in p.tts_chunks], "pieces": pieces}


@router.post("/api/tts/translated", dependencies=[Depends(deps.require_access)])
async def tts_translated(req: TranslatedTtsRequest, request: Request):
    """A chunk's translation read aloud in the chunk's own voice and delivery tags (sfx dropped), with Boson's
    `language`; no word timestamps (the whole piece is highlighted). A piece over MAX_CHARS is voiced in parts and
    joined."""
    book = _translatable_book(req.book_id)
    for p in book.paragraphs:
        for k, c in enumerate(p.tts_chunks):
            if c.id != req.chunk_id:
                continue
            voice, tts_text = _chunk_voice_and_text(book, c, req.voice_override)
            if req.cached_only and not translate.cache_path(book.id, req.language, p.idx).exists():
                raise HTTPException(409, "This translation is not pre-rendered.")
            piece = (await _pieces(book, p, req.language, request))[k]
            inputs = translate.tts_inputs(tts_text, piece, req.language, req.speed)
            cached = tts_cache.all_cached(inputs, voice, req.language)
            if req.cached_only and not cached:
                raise HTTPException(409, "This translation is not pre-rendered.")
            if not cached and not deps.tts_bucket.allow(deps.rate_key(request)):
                raise HTTPException(429, "Too many new audio requests; wait a moment.")
            try:
                out = await tts_cache.get_or_create_joined(deps.boson, inputs, voice, piece, language=req.language)
            except (BosonError, RuntimeError) as e:
                raise deps.boson_http_error(e)
            return {"chunk_id": c.id, "voice": voice, "language": req.language, "text": piece, **out}
    raise HTTPException(404, "Chunk not found")
