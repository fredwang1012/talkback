"""Talk mode: retrieval, answers (Gemini or the public API) and speaking them (Boson TTS).
Logs as "app.main" (these lines were logged from main.py before the routers split, so they read the same)."""
import json
import logging
import time
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .. import books, deps, emotion, indexes, multilingual, public_client, retrieval, tts_cache, tts_lang, users, voices, websearch
from ..retrieval import DEFAULT_K, REALTIME_K, REALTIME_MAX_PASSAGE_CHARS
from ..boson import BosonError
from ..personas import build_answer_prompt, realtime_instructions
from ..schema import Book, ChatTurn


router = APIRouter()


class RetrieveRequest(BaseModel):
    book_id: str
    query: str = Field(default="", max_length=500)
    reader_para: int = Field(ge=0)
    speaker_id: str = "narrator"
    k: int = Field(default=DEFAULT_K, ge=1, le=12)
    spoilers: bool = False  # full-book mode (novels): reader allows spoilers; default stays spoiler-safe
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)  # earlier turns with this speaker
    text_answer: bool = False  # B1 (retrieve): the Realtime answer is written as text and voiced by Higgs TTS: marker rule
    web_search: bool = False   # B2 (retrieve): the session declared Boson's web_search tool (allowed only where it cannot spoil)


def _series_context(book: Book) -> dict:
    """Keyword args for retrieval.aretrieve: earlier books of the series are fully known to the reader."""
    earlier = books.earlier_in_series(book)
    return {"earlier": frozenset(b.id for b in earlier), "recap": books.series_recap(book)}


def _resolve_talk(book_id: str, speaker_id: str, reader_para: int, spoilers: bool = False):
    """Shared 404/403 checks for talk endpoints: returns (book, speaker or None, clamped reader_para).

    Full-book mode (`spoilers`) lifts the "not met yet" gate: every character is available."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    if speaker_id != "narrator" and not any(c.id == speaker_id for c in book.characters):
        raise HTTPException(404, "Unknown speaker")
    reader_para = min(reader_para, len(book.paragraphs) - 1)
    speaker = next((c for c in book.characters if c.id == speaker_id), None)
    if speaker and speaker.main is False:
        raise HTTPException(403, f"{speaker.name} is not a main character, so you cannot talk to them.")
    if speaker and book.kind == "novel" and not spoilers and speaker.first_para > reader_para:
        raise HTTPException(403, f"{speaker.name} has not appeared yet at this point in the story.")  # server-side gate
    return book, speaker, reader_para


@router.post("/api/retrieve", dependencies=[Depends(deps.require_access)])
async def retrieve(req: RetrieveRequest, request: Request):
    if not deps.retrieve_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many lookups; wait a moment.")
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para, req.spoilers)
    # an English book: a question in another language is searched in English; a book in another language (#117): in its own
    search = await multilingual.search_query(req.query, book.language)
    # realtime (Boson) instructions must stay short: fewer passages and a smaller character cap than /api/answer
    out = await retrieval.aretrieve(book, indexes._index_for(req.book_id), search, reader_para, min(req.k, REALTIME_K),
                                    spoilers=req.spoilers, max_chars=REALTIME_MAX_PASSAGE_CHARS, **_series_context(book))
    out["instructions"] = realtime_instructions(book, req.speaker_id, out["story_so_far"], out["passages"], req.query,
                                                spoilers=req.spoilers, text_answer=req.text_answer, history=req.history,
                                                web_search=req.web_search and websearch.allowed(book.kind, req.spoilers))
    if search != req.query:
        out["search_query"] = search  # what was searched, for debugging (the browser network panel)
    out["persona"] = speaker.persona if speaker else None
    return out


# ---------- answer (Gemini) + speak (Boson TTS) for Talk mode ----------

@lru_cache(maxsize=1)
def get_answerer():
    """Lazy Gemini answerer; tests monkeypatch this to avoid Vertex."""
    from .gcp.llm import GeminiAnswerer
    return GeminiAnswerer()


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@router.post("/api/answer", dependencies=[Depends(deps.require_access)])
async def answer(req: RetrieveRequest, request: Request):
    if not deps.answer_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many questions; wait a moment.")
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para, req.spoilers)
    t0 = time.monotonic()
    search = await multilingual.search_query(req.query, book.language)  # in the book's language; answered in the reader's
    out = await retrieval.aretrieve(book, indexes._index_for(req.book_id), search, reader_para, req.k, spoilers=req.spoilers,
                                    **_series_context(book))
    t_retrieved = time.monotonic()
    retrieve_ms = int((t_retrieved - t0) * 1000)
    if deps._public_answers():  # passages above are already spoiler-filtered here; the service only writes the answer
        return StreamingResponse(_public_answer_stream(book, speaker, req, out, retrieve_ms, t_retrieved),
                                 media_type="application/x-ndjson")
    # web search only for textbooks and spoilers-on novels (never a spoiler-safe novel), within today's grounded cap
    grounded = websearch.allowed(book.kind, req.spoilers) and websearch.take_local()
    system, user = build_answer_prompt(book, req.speaker_id, out["story_so_far"], out["passages"], req.query,
                                        spoilers=req.spoilers, history=req.history, web_search=grounded)
    sources: list[dict] = []
    suggestions: list[str] = []   # Google's Search Suggestions markup (shown with the answer, as Google requires)
    search_kw = {"web_search": True, "on_sources": sources.extend, "on_suggestions": suggestions.append} if grounded else {}

    async def stream():
        first_ms = None
        try:
            async for sentence, feeling in emotion.tag_sentences(get_answerer().stream_sentences(system, user, **search_kw)):
                if first_ms is None:
                    first_ms = int((time.monotonic() - t_retrieved) * 1000)
                yield _line(emotion.sentence_line(sentence, feeling))
        except Exception as e:  # never leak stack traces or secrets to the client
            logging.getLogger("app.main").warning("answer stream failed: %s", type(e).__name__)
            yield _line({"type": "error", "message": "The answer could not be generated; please try again."})
            return
        event = websearch.sources_event(sources, suggestions[-1] if suggestions else None)
        if event:  # shown under the answer (links + Google's suggestions), never spoken
            yield _line(event)
        yield _line({"type": "done", "passages": len(out["passages"]), "retrieve_ms": retrieve_ms,
                     "first_sentence_ms": first_ms if first_ms is not None else 0})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def _public_answer_stream(book: Book, speaker, req: RetrieveRequest, out: dict, retrieve_ms: int, t_retrieved: float):
    first_ms = None
    event = None
    payload = public_client.answer_payload(book, speaker, req.speaker_id, out, req.query, req.spoilers, req.history)
    try:
        async for msg in public_client.stream_answer(payload):
            if msg.get("type") == "sources":
                event = websearch.sources_event(msg.get("sources"), msg.get("search_suggestions"))  # re-checked here
                continue
            text, marked = emotion.split_marker(msg["text"])  # the service is not trusted to have stripped markers
            if not text:
                continue
            if first_ms is None:
                first_ms = int((time.monotonic() - t_retrieved) * 1000)
            yield _line(emotion.sentence_line(text, emotion.valid(msg.get("emotion")) or marked))
    except public_client.PublicFailure as e:
        logging.getLogger("app.main").warning("public answer failed: %s", e.kind)
        if e.kind != "limit":
            public_client.mark_unavailable()  # health then reports "realtime" and the page answers through Realtime
        yield _line({"type": "error", "message": public_client.ANSWER_LIMIT if e.kind == "limit"
                     else public_client.ANSWER_UNAVAILABLE})
        return
    if event:
        yield _line(event)
    yield _line({"type": "done", "passages": len(out["passages"]), "retrieve_ms": retrieve_ms,
                 "first_sentence_ms": first_ms if first_ms is not None else 0})


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: str
    emotion: str | None = Field(default=None, max_length=40)  # an answer sentence's feeling (#14); unknown is ignored
    language: str | None = Field(default=None, max_length=3)  # ISO 639-3 of the sentence (#135); None / 'eng' = English

    @field_validator("language")
    @classmethod
    def _documented_language(cls, v):
        if v is not None and not tts_lang.valid(v):
            raise ValueError("language must be an ISO 639-3 code of a language Higgs TTS supports")
        return v


@router.post("/api/speak", dependencies=[Depends(deps.require_access)])
async def speak(req: SpeakRequest, request: Request):
    if not voices.is_known_voice(req.voice):
        raise HTTPException(400, f"Unknown voice '{req.voice}'.")
    text = emotion.strip_tags(req.text)   # answer / recap text: it never carries its own Boson tags
    if not text:
        raise HTTPException(400, "Nothing to say.")
    tts_text = emotion.tts_input(text, emotion.valid(req.emotion))
    lang = None if tts_lang.is_english(req.language) else req.language
    if not tts_cache.is_cached(tts_text, req.voice, *([lang] if lang else [])) and not deps.tts_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many new audio requests; wait a moment.")
    try:
        out = await tts_cache.get_or_create(deps.boson, tts_text, req.voice, text, **({"language": lang} if lang else {}))
    except (BosonError, RuntimeError) as e:
        raise deps.boson_http_error(e)
    return {"voice": req.voice, **out}
