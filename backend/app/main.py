import asyncio
import base64
import gzip
import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import AsyncExitStack, asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from pydantic import BaseModel, Field, field_validator

from . import books, cast, emotion, face_detect, faces, ingest_upload, multilingual, public_client, recap, retrieval, tts_cache, tts_lang, users, voices
from .delivery import apply_speed
from .retrieval import DEFAULT_K, REALTIME_K, REALTIME_MAX_PASSAGE_CHARS
from .boson import BosonClient, BosonError
from .config import settings
from .enrich import describe as describe_book
from .enrich import equations as enrich_equations
from .enrich import pipeline as enrich_pipeline
from .enrich.detect import detect_kind
from .gcp import usage
from .personas import build_answer_prompt, build_instructions
from . import websearch
from .mp3dur import mp3_seconds
from .ratelimit import TokenBucket, client_ip
from .schema import Book, ChatTurn
from .ttschunks import inline_spans
from .uploadlimit import UploadSizeLimit

def _warm_up() -> None:
    """Load the embedding model and every book's index before the first question (the model takes ~17 s to load)."""
    if settings.auto_rechunk:
        books.schedule_all_upgrades()  # out-of-date tts_chunks are rebuilt on a background thread (no API calls)
    try:
        for info in books.list_books():
            _index_for(info["id"])
        if settings.retrieval_backend == "vertex":  # first Vertex call pays auth + TLS setup (~5 s): pay it now
            _vertex_deps()[0].embed_query("warm up")
    except Exception:  # never block startup; retrieval falls back to BM25 / loads lazily
        pass
    # after the indexes: with the LLM down a check can wait for a timeout, which must not delay the first question
    try:  # uploads whose first volume got no series (#134): one bounded, once-per-book pass; never raises
        from .enrich import series_link
        asyncio.run(series_link.startup_default())
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(users.migrate_on_startup)  # PER_USER only; never raises
    task = asyncio.create_task(asyncio.to_thread(_warm_up))
    resume = asyncio.create_task(_resume_enrichments())   # no job survives a restart: pick up the interrupted ones
    reindex = asyncio.create_task(_retry_failed_indexes())   # Vertex indexes a transient error left in state "error"
    yield
    task.cancel()
    resume.cancel()
    reindex.cancel()


app = FastAPI(title="Talkback", lifespan=lifespan, dependencies=[Depends(users.bind_identity)])
app.add_middleware(UploadSizeLimit)  # added before CORS so CORS headers still wrap its 4xx replies
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

(settings.data_dir / "cache" / "tts").mkdir(parents=True, exist_ok=True)
# Only the TTS audio is public: the rest of data/cache holds the usage log and the LLM cache.
app.mount("/media/tts", StaticFiles(directory=settings.data_dir / "cache" / "tts"), name="media")


def _lipsync_dir() -> Path:
    """Lip-sync clips: shared cache/avatar/talk, or the caller's own folder with PER_USER."""
    return users.lipsync_dir()


class LipsyncFiles(StaticFiles):
    """/media/avatar. With PER_USER a caller is served only their own lip-sync clips (a face is a real person's)."""

    async def get_response(self, path: str, scope):
        if settings.per_user:
            ident = await users.identify_async(Headers(scope=scope))
            return await StaticFiles(directory=users.lipsync_dir(ident), check_dir=False).get_response(path, scope)
        return await super().get_response(path, scope)


_lipsync_dir().mkdir(parents=True, exist_ok=True)
app.mount("/media/avatar", LipsyncFiles(directory=_lipsync_dir()), name="lipsync")


class PortraitFiles(StaticFiles):
    """/portraits. With PER_USER the shared `faces` and `voices` folders are never served; /portraits/faces/<file>
    is answered from the caller's own face folder, and a book's images only to callers who may see that book."""

    async def get_response(self, path: str, scope):
        if settings.per_user:
            ident = await users.identify_async(Headers(scope=scope))
            head, _, rest = path.replace("\\", "/").partition("/")  # StaticFiles hands over OS-style paths
            if head == "voices":
                raise HTTPException(404)
            if head == "faces":
                return await StaticFiles(directory=users.faces_dir(ident), check_dir=False).get_response(rest, scope)
            if head and not users.can_see_book(head, ident):
                raise HTTPException(404)
            return await StaticFiles(directory=settings.data_dir / "portraits", check_dir=False).get_response(path, scope)
        return await super().get_response(path, scope)


app.mount("/portraits", PortraitFiles(directory=settings.data_dir / "portraits", check_dir=False), name="portraits")

boson = BosonClient()
token_bucket = TokenBucket(capacity=10, per_seconds=60)
tts_bucket = TokenBucket(capacity=120, per_seconds=60)   # only cache misses (real Boson calls) consume this
clone_bucket = TokenBucket(capacity=5, per_seconds=60)
answer_bucket = TokenBucket(capacity=20, per_seconds=60)  # every /api/answer call spends Gemini credit


retrieve_bucket = TokenBucket(capacity=30, per_seconds=60)  # vertex retrieval spends embedding + search credit


def rate_key(request: Request) -> str:
    """Rate-limit key: the signed-in account with PER_USER, else the client address."""
    return users.rate_key(request, client_ip(request))


def require_access(x_talkback_token: str | None = Header(default=None)) -> None:
    """Opt-in gate for endpoints that spend Boson credit: set TALKBACK_ACCESS_TOKEN for any public deployment."""
    expected = settings.access_token
    if expected and not secrets.compare_digest(x_talkback_token or "", expected):
        raise HTTPException(401, "Missing or invalid access token.")


def boson_http_error(e: BosonError | RuntimeError) -> HTTPException:
    """Turn Boson failures into readable API errors. insufficient_quota is a billing problem, not a retry."""
    if isinstance(e, RuntimeError):
        return HTTPException(503, str(e))
    if e.type == "insufficient_quota":
        return HTTPException(402, f"Boson billing refused the request ({e.code or 'insufficient_quota'}): {e.message}")
    if e.type == "rate_limit_error":
        return HTTPException(429, "Boson is rate limiting requests; try again shortly.")
    if e.status in (400, 403):
        return HTTPException(502, f"Boson rejected the request: {e.message}")
    return HTTPException(502, f"Boson error ({e.type}): {e.message}")


def _public_answers() -> bool:
    """Typed/Gemini answers go through the public API: it is configured and this machine has no LLM of its own."""
    return settings.answer_backend is None and settings.retrieval_backend != "vertex" and public_client.active()


def answer_backend() -> str:
    """"gemini" when /api/answer writes the answers (locally, or through the public API while it answers), else "realtime"."""
    if _public_answers() and public_client.answers_available():
        return "gemini"
    return settings.effective_answer_backend


@app.get("/api/health")
def health():
    return {"ok": True, "boson_key_set": bool(settings.boson_api_key), "access_required": bool(settings.access_token),
            "answer_backend": answer_backend()}


@app.get("/api/me")
def me():
    ident = users.current()
    return {"email": ident.email if settings.per_user else None, "per_user": settings.per_user}


@app.get("/api/books")
def get_books():
    return books.list_books()


@app.get("/api/hidden-books")
def get_hidden_books():
    """Built-in books the caller hid from the library (restorable)."""
    return [{"id": b["id"], "title": b["title"], "author": b["author"]} for b in books.list_books(hidden=True)]


def _builtin_for_account(book_id: str) -> None:
    """Hide/restore: a visible built-in book, and (PER_USER) a signed-in account so nobody hides it for everyone."""
    if not books._ID.match(book_id):
        raise HTTPException(404, "Book not found")
    users.guard_book(book_id)
    users.require_account()
    if book_id.startswith("u-"):
        raise HTTPException(403, "Books you uploaded are deleted, not hidden.")
    if not (settings.data_dir / "books" / book_id / "book.json").is_file():
        raise HTTPException(404, "Book not found")


@app.post("/api/books/{book_id}/hide", dependencies=[Depends(require_access)])
def hide_book(book_id: str):
    """Remove a built-in book (classics, Harry Potter) from this account's library. Its files are never touched:
    they are shared, and their processing was expensive."""
    _builtin_for_account(book_id)
    users.set_hidden(book_id, True)
    return {"hidden": book_id}


@app.post("/api/books/{book_id}/restore", dependencies=[Depends(require_access)])
def restore_book(book_id: str):
    _builtin_for_account(book_id)
    users.set_hidden(book_id, False)
    return {"restored": book_id}


_book_bodies: dict[str, tuple[Book, bytes, bytes]] = {}  # book id -> (book object, json, gzipped json)


_FIGURE_NAME = re.compile(r"^fig-\d{1,4}\.png$")


@app.get("/api/books/{book_id}/figures/{name}", dependencies=[Depends(require_access)])
def get_figure(book_id: str, name: str):
    """A textbook figure (#40), gated like the book itself: the same access code and per-account visibility."""
    if not books._ID.match(book_id) or not _FIGURE_NAME.match(name):  # both come from the URL: no path walking
        raise HTTPException(404, "Figure not found")
    users.guard_book(book_id)
    path = ingest_upload.figures_dir(book_id) / name
    if not path.is_file():
        raise HTTPException(404, "Figure not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/books/{book_id}", response_model=Book, dependencies=[Depends(require_access)])
def get_book(book_id: str, accept_encoding: str = Header(default="")):
    """The full book. Gated like the paid endpoints: a public deployment must not hand out a copyrighted text.
    Serialised and gzipped once per book version (a novel is several MB of JSON)."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    cached = _book_bodies.get(book_id)
    if cached is None or cached[0] is not book:  # load_book returns a new object when book.json changes
        body = book.model_dump_json().encode()
        cached = _book_bodies[book_id] = (book, body, gzip.compress(body, compresslevel=6))
    if "gzip" in accept_encoding.lower():
        return Response(cached[2], media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(cached[1], media_type="application/json")


@app.get("/api/books/{book_id}/recap", dependencies=[Depends(require_access)])
def get_recap(book_id: str, para: int = 0, scope: recap.Scope | None = None,
              section: str | None = Query(None, max_length=80)):
    """Spoiler-safe "Previously..." for the reader's place: the latest stored summary written from text at or
    before `para` (never a later one), the chapter they are in, and the closing summaries of earlier books.
    With `scope` (chapter | book | series, see recap.py) just that recap: {scope, upto_para, chapter, text}; with
    `section` too, that chapter's recap (a textbook: any section; a novel: only as far as the reader has read)."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    _, para = retrieval._clamp(book, para)
    if scope is not None:
        return recap.scoped(book, para, scope, section)
    ok = [s for s in book.summaries if s.upto_para <= para]
    best = max(ok, key=lambda s: s.upto_para) if ok else None
    sections = [s for s in book.sections if s.start_para <= para]
    chapter = max(sections, key=lambda s: s.start_para).title if sections else ""
    return {"upto_para": best.upto_para if best else None, "chapter": chapter,
            "text": best.text if best else "", "earlier": books.series_recap(book),
            "earlier_full": books.series_recap(book, full=True)}


# ---------- realtime key ----------

@app.post("/api/realtime/token", dependencies=[Depends(require_access)])
async def realtime_token(request: Request):
    """Mint a fresh ephemeral Realtime key per call (never cached). The real API key never leaves the server."""
    if not token_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many connection attempts; wait a moment.")
    try:
        secret = await boson.mint_client_secret(600)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
    usage.record("boson_realtime_session", "boson-realtime", sessions=1)
    return secret


# ---------- TTS ----------

class TtsRequest(BaseModel):
    book_id: str
    chunk_id: str
    voice_override: str | None = None
    cached_only: bool = False  # demo mode: serve pre-rendered audio only, never call Boson
    speed: Literal["very_slow", "slow", "fast", "very_fast"] | None = None  # natural speed (#78): Boson speed tag, own cache entry


@app.post("/api/tts", dependencies=[Depends(require_access)])
async def tts(req: TtsRequest, request: Request):
    users.guard_book(req.book_id)
    book = books.load_book(req.book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    for p in book.paragraphs:
        for c in p.tts_chunks:
            if c.id == req.chunk_id:
                voice = req.voice_override or cast.voice_for(book, c.speaker) or c.voice
                if not voices.is_known_voice(voice):
                    raise HTTPException(400, f"Unknown voice '{voice}'.")
                tts_text = c.tts_text
                ch = next((x for x in book.characters if x.id == c.speaker), None)
                tags = cast.tags_for(book, c.speaker) if ch else None
                if tags is not None and tts_text.startswith(ch.voice_tags):  # the user's pitch/speed replaces the book's
                    tts_text = tags + tts_text[len(ch.voice_tags):]
                tts_text = apply_speed(tts_text, req.speed)
                cached = tts_cache.is_cached(tts_text, voice)
                if req.cached_only and not cached:
                    raise HTTPException(409, "This line is not pre-rendered yet. Run preprocess/prerender_tts.py before the demo.")
                if not cached and not tts_bucket.allow(rate_key(request)):
                    raise HTTPException(429, "Too many new audio requests; wait a moment.")
                try:
                    out = await tts_cache.get_or_create(boson, tts_text, voice, p.text[c.char_start:c.char_end],
                                                        inline_spans(p, c.char_start, c.char_end))
                except (BosonError, RuntimeError) as e:
                    raise boson_http_error(e)
                return {"chunk_id": c.id, "voice": voice, **out}
    raise HTTPException(404, "Chunk not found")


# ---------- retrieval for Talk mode ----------

class RetrieveRequest(BaseModel):
    book_id: str
    query: str = Field(default="", max_length=500)
    reader_para: int = Field(ge=0)
    speaker_id: str = "narrator"
    k: int = Field(default=DEFAULT_K, ge=1, le=12)
    spoilers: bool = False  # full-book mode (novels): reader allows spoilers; default stays spoiler-safe
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)  # earlier turns with this speaker (answer only)


@lru_cache(maxsize=1)
def _vertex_deps():
    """Shared embedder + vector store, built on first use (no network at import or construction of the index)."""
    from .gcp.embed import GeminiEmbedder
    from .gcp.vectorstore import VectorStore
    return GeminiEmbedder(), VectorStore(settings.vs_collection_path)


def _vertex_index_ready(book_id: str) -> bool:
    """False while a book's Vertex index is building, failed or was interrupted (retrieval then uses local BM25).
    Books with no index progress recorded (seeded or indexed offline) are served from Vertex."""
    idx = (ingest_upload.read_status(book_id) or {}).get("index")
    return not idx or idx.get("state") == "done"


@lru_cache(maxsize=8)
def _index_for(book_id: str) -> retrieval.Index:
    book = books.load_book(book_id)
    if settings.retrieval_backend == "vertex" and _vertex_index_ready(book_id):
        from .gcp.vertex_index import VertexIndex
        earlier = {b.id: b.title for b in books.earlier_in_series(book)}
        return VertexIndex(book, _LazyEmbedder(), _LazyStore(), earlier)
    index_dir = settings.data_dir / "books" / book_id / "index"
    if settings.retrieval_backend == "vertex":  # Vertex index still building (or failed): local BM25, same eligibility
        if (index_dir / "chunks.json").exists():
            return retrieval.Index.load(index_dir)
        return retrieval.Index(retrieval.make_chunks(book))
    if (index_dir / "chunks.json").exists():
        return retrieval.Index.load(index_dir, embedder=_embedder())
    return retrieval.Index(retrieval.make_chunks(book))  # BM25-only until the index is built


class _LazyEmbedder:
    def embed_query(self, text: str):
        return _vertex_deps()[0].embed_query(text)


class _LazyStore:
    async def vector_search(self, *a, **kw):
        return await _vertex_deps()[1].vector_search(*a, **kw)

    async def text_search(self, *a, **kw):
        return await _vertex_deps()[1].text_search(*a, **kw)


@lru_cache(maxsize=1)
def _embedder():
    try:
        from .embedder import E5Embedder
        return E5Embedder()
    except Exception:  # sentence-transformers not installed: fall back to BM25
        return None


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


@app.post("/api/retrieve", dependencies=[Depends(require_access)])
async def retrieve(req: RetrieveRequest, request: Request):
    if not retrieve_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many lookups; wait a moment.")
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para, req.spoilers)
    search = await multilingual.search_query(req.query)  # the book is English: a question in another language is translated
    # realtime (Boson) instructions must stay short: fewer passages and a smaller character cap than /api/answer
    out = await retrieval.aretrieve(book, _index_for(req.book_id), search, reader_para, min(req.k, REALTIME_K),
                                    spoilers=req.spoilers, max_chars=REALTIME_MAX_PASSAGE_CHARS, **_series_context(book))
    out["instructions"] = build_instructions(book, req.speaker_id, out["story_so_far"], out["passages"], spoilers=req.spoilers,
                                             reply_language=multilingual.foreign_language(req.query), voice_emotion=True)
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


@app.post("/api/answer", dependencies=[Depends(require_access)])
async def answer(req: RetrieveRequest, request: Request):
    if not answer_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many questions; wait a moment.")
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para, req.spoilers)
    t0 = time.monotonic()
    search = await multilingual.search_query(req.query)  # searched in English; answered in the reader's language
    out = await retrieval.aretrieve(book, _index_for(req.book_id), search, reader_para, req.k, spoilers=req.spoilers,
                                    **_series_context(book))
    t_retrieved = time.monotonic()
    retrieve_ms = int((t_retrieved - t0) * 1000)
    if _public_answers():  # passages above are already spoiler-filtered here; the service only writes the answer
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
            logging.getLogger(__name__).warning("answer stream failed: %s", type(e).__name__)
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
        logging.getLogger(__name__).warning("public answer failed: %s", e.kind)
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


@app.post("/api/speak", dependencies=[Depends(require_access)])
async def speak(req: SpeakRequest, request: Request):
    if not voices.is_known_voice(req.voice):
        raise HTTPException(400, f"Unknown voice '{req.voice}'.")
    text = emotion.strip_tags(req.text)   # answer / recap text: it never carries its own Boson tags
    if not text:
        raise HTTPException(400, "Nothing to say.")
    tts_text = emotion.tts_input(text, emotion.valid(req.emotion))
    lang = None if tts_lang.is_english(req.language) else req.language
    if not tts_cache.is_cached(tts_text, req.voice, *([lang] if lang else [])) and not tts_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many new audio requests; wait a moment.")
    try:
        out = await tts_cache.get_or_create(boson, tts_text, req.voice, text, **({"language": lang} if lang else {}))
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
    return {"voice": req.voice, **out}


# ---------- user uploads (PDF / .txt -> narrator-only book) ----------

_upload_status: dict[str, dict] = {}
upload_bucket = TokenBucket(capacity=6, per_seconds=60)  # uploads per IP (a multi-file drop; UPLOAD_QUEUE_MAX)


UPLOAD_QUEUE_MAX = 6      # uploads accepted and not yet readable (each holds its bytes in memory until parsed)
INGEST_CONCURRENCY = 2    # of those, parsed at once (a 20 MB PDF is a lot of CPU and memory)
INDEX_CONCURRENCY = 2     # Vertex indexing jobs at once across uploads and enrichment; the rest wait as "queued"


class IngestGuard:
    """Bounds the uploads in flight (accepted, not yet published) to `capacity`; each upload holds one token.

    When full, a hold older than STALE_SECONDS may be taken over only if its background task never started (it is
    dead). A task that started and has not released is still running (a thread cannot be killed), so it keeps its
    slot. Release only frees the caller's own token.
    """
    STALE_SECONDS = 15 * 60

    def __init__(self, clock=time.monotonic, capacity: int = UPLOAD_QUEUE_MAX):
        self.clock, self.capacity = clock, capacity
        self._holds: dict[int, list] = {}   # token -> [since, started]
        self._token = 0
        self._warned = False

    @property
    def busy(self) -> bool:
        return bool(self._holds)

    def acquire(self) -> int | None:
        if len(self._holds) >= self.capacity:
            now = self.clock()
            dead = [t for t, (since, started) in self._holds.items() if not started and now - since >= self.STALE_SECONDS]
            log = logging.getLogger(__name__)
            if not dead:
                if not self._warned and any(now - since >= self.STALE_SECONDS for since, _ in self._holds.values()):
                    self._warned = True
                    log.warning("an ingest has been running for over 15 minutes; its upload slot stays taken")
                return None
            log.warning("an upload slot was held for over 15 minutes by a task that never started; taking it over")
            self._holds.pop(min(dead))
        self._token += 1
        self._holds[self._token] = [self.clock(), False]
        return self._token

    def mark_started(self, token: int) -> None:
        if token in self._holds:
            self._holds[token][1] = True

    def release(self, token: int) -> None:
        self._holds.pop(token, None)
        if not self._holds:
            self._warned = False


def _loop_semaphore(n: int):
    """A semaphore per event loop (asyncio primitives bind to the loop they first wait on; tests run several)."""
    cache: dict = {}

    def get() -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if cache.get("loop") is not loop:
            cache.update(loop=loop, sem=asyncio.Semaphore(n))
        return cache["sem"]
    return get


_ingest_slot = _loop_semaphore(INGEST_CONCURRENCY)
_index_slot = _loop_semaphore(INDEX_CONCURRENCY)


_ingest_guard = IngestGuard()


# Books deleted by this process (ids are never reused): background jobs check it so they never write a deleted book
# back to disk or start paid work on it.
_deleted: set[str] = set()


class _BookDeleted(Exception):
    """Raised from an indexing progress callback to stop the job once its book has been deleted."""


def _mem_index(book_id: str) -> dict | None:
    """The in-memory indexing progress (newer than status.json, whose progress writes are throttled)."""
    return (_upload_status.get(book_id) or {}).get("index")


def _set_status(book_id: str, status: str, message: str | None = None, **kind_info) -> None:
    if book_id in _deleted:
        return
    _upload_status[book_id] = ingest_upload.write_status(book_id, status, message, index=_mem_index(book_id),
                                                         **kind_info)


def _index_status(book_id: str, fields: dict) -> dict:
    """The current status with its indexing progress replaced; everything else (enrich, kind) is kept."""
    cur = _upload_status.get(book_id) or ingest_upload.read_status(book_id) or {}
    return {**{k: v for k, v in cur.items() if k != "index"}, "status": "ready", "index": fields}


def _set_index(book_id: str, **fields) -> None:
    """Record the Vertex indexing progress {state, done, total, message?} next to the (ready) status (sync write)."""
    if book_id in _deleted:
        return
    _upload_status[book_id] = st = _index_status(book_id, fields)
    ingest_upload.save_status(book_id, st)
    _index_written_at[book_id] = time.monotonic()


INDEX_WRITE_EVERY_S = 2.0   # status.json lives on a GCS FUSE mount: progress is written at most this often
_index_written_at: dict[str, float] = {}
_index_writes: dict[str, asyncio.Future] = {}


def _write_current_status(book_id: str) -> None:
    """Worker thread: write the latest in-memory status (read now, so a newer enrich update is not overwritten)."""
    st = _upload_status.get(book_id)
    if st is not None and book_id not in _deleted:
        ingest_upload.save_status(book_id, st)


def _index_progress(book_id: str, done: int, total: int) -> None:
    """index_book's progress callback (on the loop): memory always, status.json throttled and off the loop."""
    if book_id in _deleted:
        raise _BookDeleted(book_id)
    _upload_status[book_id] = _index_status(book_id, {"state": "running", "done": done, "total": total})
    pending = _index_writes.get(book_id)
    now = time.monotonic()
    if (pending is not None and not pending.done()) or now - _index_written_at.get(book_id, 0.0) < INDEX_WRITE_EVERY_S:
        return
    _index_written_at[book_id] = now
    _index_writes[book_id] = asyncio.ensure_future(asyncio.to_thread(_write_current_status, book_id))


# Each book is its own indexing job; at most INDEX_CONCURRENCY run at once (the upload guard is released once a book is
# published, so without a limit many uploads could each start a paid embedding job at once) and one book never has
# two jobs (no duplicate paid indexing).
_index_jobs: set[str] = set()


async def _index_in_background(book: Book, chunks) -> None:
    """Index a published book into Vertex; the book is readable (BM25) meanwhile. Never raises."""
    book_id, total = book.id, len(chunks)
    if book_id in _index_jobs:   # already queued or running for this book
        return
    _index_jobs.add(book_id)
    final: dict | None = None
    try:
        _set_index(book_id, state="queued", done=0, total=total)
        async with _index_slot():
            if book_id in _deleted:   # deleted while queued: nothing to pay for
                return
            _set_index(book_id, state="running", done=0, total=total)
            embedder, store = _vertex_deps()
            from .gcp.indexing import index_book
            await index_book(book, chunks, embedder, store,
                             on_progress=lambda done, tot: _index_progress(book_id, done, tot))
        final = {"state": "done", "done": total, "total": total}
    except _BookDeleted:
        pass
    except Exception as e:  # the book stays readable and keeps the BM25 fallback
        if book_id not in _deleted:
            logging.getLogger(__name__).warning("vertex indexing failed: %s", type(e).__name__)
            final = {"state": "error", "done": 0, "total": total, "message": INDEX_FAILED_MESSAGE}
    finally:
        _index_jobs.discard(book_id)
        _index_for.cache_clear()
        pending = _index_writes.pop(book_id, None)
        if pending is not None:   # a throttled progress write must not land after the final one
            await asyncio.gather(pending, return_exceptions=True)
        _index_written_at.pop(book_id, None)
    if final is not None and book_id not in _deleted:
        _upload_status[book_id] = _index_status(book_id, final)
        await asyncio.to_thread(_write_current_status, book_id)


def _set_enrich(book_id: str, **fields) -> None:
    """Record the novel enrichment sub-status next to the (ready) ingest status."""
    if book_id in _deleted:
        return
    retries = fields.pop("enrich_retries", None)
    enrich = {k: v for k, v in fields.items() if v is not None}
    extra = {"enrich_retries": retries} if retries is not None else {}
    _upload_status[book_id] = ingest_upload.write_status(book_id, "ready", enrich=enrich, index=_mem_index(book_id), **extra)


_enrich_lock = asyncio.Semaphore(max(1, settings.enrich_parallel))  # ENRICH_PARALLEL uploads at a time (rate limits)


INDEX_FAILED_MESSAGE = "Search indexing failed; using basic search."


async def _enrich_indexer(book: Book) -> None:
    """Final stage: refresh the Vertex index with the speakers (the local index needs no change). A failure (after the
    store's and embedder's own retries) is recorded as index state "error", so retrieval falls back to BM25 and the
    next start retries it (_retry_failed_indexes); it is then raised for the pipeline, which finishes the book anyway."""
    if settings.retrieval_backend == "vertex":
        from .gcp.indexing import index_book, load_chunks
        embedder, store = _vertex_deps()
        chunks = await asyncio.to_thread(load_chunks, book)
        while book.id in _index_jobs:   # the upload's own indexing job is still queued or running: never overlap it
            await asyncio.sleep(1.0)
        _index_jobs.add(book.id)
        try:
            async with _index_slot():
                await index_book(book, chunks, embedder, store)
        except Exception as e:
            if book.id not in _deleted:
                logging.getLogger(__name__).warning("vertex re-indexing failed: %s", type(e).__name__)
                _set_index(book.id, state="error", done=0, total=len(chunks), message=INDEX_FAILED_MESSAGE)
            raise
        finally:
            _index_jobs.discard(book.id)
            _index_for.cache_clear()


MAX_STARTUP_REINDEX = 20   # failed indexes retried per start (each is a paid embedding job)


def _failed_indexes() -> list[str]:
    """Uploads whose status.json says the Vertex index failed (a transient error at upload or enrichment time)."""
    out = []
    for path in sorted((settings.data_dir / "books").glob("u-*/status.json")):
        book_id = path.parent.name
        if books._ID.match(book_id) and ((ingest_upload.read_status(book_id) or {}).get("index") or {}).get("state") == "error":
            out.append(book_id)
    return out


async def _retry_failed_indexes() -> None:
    """Startup: rebuild the Vertex index of books whose last indexing failed, at most MAX_STARTUP_REINDEX of them and
    once per start (a book that fails again waits for the next start). Never raises."""
    await asyncio.to_thread(_settle_all_index_only_failures)
    if settings.retrieval_backend != "vertex":
        return
    try:
        from .gcp.indexing import load_chunks
        for book_id in (await asyncio.to_thread(_failed_indexes))[:MAX_STARTUP_REINDEX]:
            if book_id in _deleted or book_id in _index_jobs:
                continue
            book = await asyncio.to_thread(books.load_book, book_id)
            if book is None:
                continue
            _settle_index_only_failure(book_id, book)
            chunks = await asyncio.to_thread(load_chunks, book)
            await _index_in_background(book, chunks)   # never raises; records done or error in status.json
            if (book_status_or_empty(book_id).get("index") or {}).get("state") == "done":
                en = book_status_or_empty(book_id).get("enrich") or {}
                if en.get("status") == "done" and en.get("message") == enrich_pipeline.INDEX_PENDING:
                    _set_enrich(book_id, status="done", stage="done", progress=1.0, message=enrich_pipeline.READY)
    except Exception as e:
        logging.getLogger(__name__).warning("retrying failed indexes failed: %s", type(e).__name__)


def book_status_or_empty(book_id: str) -> dict:
    """The stored status without the per-user guard (startup work has no request identity)."""
    try:
        return _upload_status.get(book_id) or ingest_upload.read_status(book_id) or {}
    except Exception:
        return {}


def _settle_all_index_only_failures() -> None:
    """Startup: every upload an older version left "stopped early" at the index step, whatever its index state now."""
    for path in sorted((settings.data_dir / "books").glob("u-*/status.json")):
        book_id = path.parent.name
        try:
            en = (ingest_upload.read_status(book_id) or {}).get("enrich") or {}
            if en.get("status") != "error" or en.get("stage") != "index":
                continue
            book = books.load_book(book_id)
            if book is not None:
                _settle_index_only_failure(book_id, book)
        except Exception as e:
            logging.getLogger(__name__).warning("settling %s failed: %s", book_id, type(e).__name__)


def _settle_index_only_failure(book_id: str, book) -> None:
    """A book an older version marked "stopped early" only because its last step (indexing) failed: its characters
    and recap are saved, so it is done; search uses basic mode until the index is rebuilt."""
    en = book_status_or_empty(book_id).get("enrich") or {}
    if en.get("status") == "error" and en.get("stage") == "index" and book.characters and book.summaries:
        indexed = ((book_status_or_empty(book_id).get("index") or {}).get("state") == "done"
                   or settings.retrieval_backend != "vertex")
        _set_enrich(book_id, status="done", stage="done", progress=1.0,
                    message=enrich_pipeline.READY if indexed else enrich_pipeline.INDEX_PENDING)


MAX_ENRICH_RETRIES = 3          # user retries per book (persisted in status.json as enrich_retries)
TOO_MANY_TRIES = "Character processing has failed several times; the book stays readable with the narrator."
ENRICH_OFF = "Character processing is turned off on this server; the book stays readable with the narrator."
_enrich_jobs: set[str] = set()   # books with an enrichment job queued or running
_kind_claims: set[str] = set()   # books whose kind change is being handled (check-and-claim, see change_kind)


async def _run_enrichment(book_id: str, resume: bool = False) -> None:
    """resume (a retry or a restart's resume): the pipeline reuses the stages an earlier run already saved."""
    if book_id in _enrich_jobs:  # defence in depth: never two paid jobs for one book
        return
    _enrich_jobs.add(book_id)
    try:
        _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start")
        async with _enrich_lock:
            book = books.load_book(book_id)
            if book is not None and book.kind == "textbook":   # recap notes only (no characters, no re-index)
                report = lambda **f: _set_enrich(book_id, summaries_only=True, **f)  # noqa: E731
                if recap.textbook_notes(book):  # already written: nothing to pay for
                    report(status="done", stage="done", progress=1.0, message=enrich_pipeline.TEXTBOOK_DONE)
                    return
                run = public_client.summarize_textbook if public_client.active() else enrich_pipeline.enrich_textbook
                await run(book_id, report)
                return
            if book is not None and book.characters and (book.summaries or not resume):  # already enriched: nothing to pay for
                _set_enrich(book_id, status="done", stage="done", progress=1.0,
                            message="Characters, recap and search are ready.")
                return
            if public_client.active():
                await public_client.enrich_book(book_id, lambda **f: _set_enrich(book_id, **f), indexer=_enrich_indexer)
            else:
                await enrich_pipeline.enrich_book(book_id, lambda **f: _set_enrich(book_id, **f), indexer=_enrich_indexer,
                                                  resume=resume)
    finally:
        _enrich_jobs.discard(book_id)
        _index_for.cache_clear()


_resume_tasks: set[asyncio.Task] = set()


def _stale_enrichments() -> list[str]:
    """Uploads whose status.json says the character processing is queued or running: after a restart, nothing runs."""
    out = []
    for path in sorted((settings.data_dir / "books").glob("u-*/status.json")):
        book_id = path.parent.name
        st = ingest_upload.read_status(book_id) or {}
        if books._ID.match(book_id) and (st.get("enrich") or {}).get("status") in ("running", "queued"):
            out.append(book_id)
    return out


async def _resume_one(book_id: str, ns: str) -> None:
    """Run the interrupted job as its owner (the PER_USER namespace is a context variable, not a global)."""
    token = users._current.set(users.Identity(None, ns)) if ns else None
    try:
        await _run_enrichment(book_id, resume=True)
    finally:
        if token is not None:
            users._current.reset(token)


async def _resume_enrichments() -> None:
    """Startup: restart the enrichment of uploads that a restart interrupted. Same caps and the same single
    enrichment slot as a fresh job; stages already saved are not paid for again. With PER_USER on, the owner comes
    from the book's owner marker; when it cannot be determined the book is left retryable instead of run. Never raises."""
    try:
        todo = await asyncio.to_thread(_stale_enrichments)
        jobs = []
        for book_id in todo:
            if book_id in _enrich_jobs or book_id in _kind_claims or book_id in _deleted:
                continue
            if not settings.upload_enrich:   # UPLOAD_ENRICH=false: never pay for a resume
                _set_enrich(book_id, status="error", message=ENRICH_OFF)
                continue
            book = await asyncio.to_thread(books.load_book, book_id)
            if book is None:
                continue
            ns = ""
            if settings.per_user:
                ns = users.book_owner_ns(book_id) or ""
                if not ns:   # unknown owner: never run it as somebody else
                    _set_enrich(book_id, status="error", message=enrich_pipeline.INTERRUPTED)
                    continue
            textbook = book.kind == "textbook"
            flag = {"summaries_only": True} if textbook else {}
            # a resume is an attempt, counted BEFORE it runs: a book that kills the process is not resumed forever
            tries = int((await asyncio.to_thread(ingest_upload.read_status, book_id) or {}).get("enrich_retries") or 0)
            if tries >= MAX_ENRICH_RETRIES:
                _set_enrich(book_id, status="error", message=TOO_MANY_TRIES, **flag)
                continue
            reason = enrich_pipeline.check_caps(enrich_pipeline.estimate_for(book), textbook=textbook, book_id=book_id)
            if reason:
                _set_enrich(book_id, status="refused", message=reason, **flag)
                continue
            _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start", **flag,
                        enrich_retries=tries + 1)
            jobs.append(asyncio.create_task(_resume_one(book_id, ns)))   # the enrichment lock lets one run at a time
        _resume_tasks.update(jobs)
        for t in jobs:
            t.add_done_callback(_resume_tasks.discard)
        await asyncio.gather(*jobs, return_exceptions=True)
    except Exception as e:
        logging.getLogger(__name__).warning("resuming enrichment failed: %s", type(e).__name__)


def _decide_enrichment(book_id: str, book: Book) -> bool:
    """Refuse (caps), wait for confirmation (testing mode) or mark as queued. True when the job should now run.
    A textbook gets only recap notes (enrich/textbook.py), with the same estimate-first caps."""
    textbook = book.kind == "textbook"
    flag = {"summaries_only": True} if textbook else {}
    est = enrich_pipeline.estimate_for(book)
    reason = enrich_pipeline.check_caps(est, textbook=textbook, book_id=book_id)
    if reason:
        _set_enrich(book_id, status="refused", message=reason, **flag)
    elif settings.upload_confirm:
        _set_enrich(book_id, status="awaiting_confirmation", estimate_usd=round(est, 2), **flag,
                    message=enrich_pipeline.TEXTBOOK_CONFIRM if textbook else "Characters, voices and recap need processing.")
    else:
        _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start", **flag)
        return True
    return False


async def _plan_enrichment(book_id: str, book: Book) -> None:
    """After the text ingest of a novel: refuse (caps), wait for confirmation (testing mode) or start."""
    if _decide_enrichment(book_id, book):
        await _run_enrichment(book_id)


async def _ingest(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str, token: int,
                  hash_key: tuple[str, str] | None = None) -> None:
    """Background ingest: CPU work in a thread, then publish book.json (readable, local BM25 retrieval), then (vertex
    only) index the chunks in the background with progress in status.json["index"].

    book.json is written after the CPU work, so a processing or failed upload is never listed or readable; a Vertex
    indexing failure keeps the book readable. Never raises. A novel is then enriched (cast, dialogue, summaries) while it is already readable with the narrator.
    """
    _ingest_guard.mark_started(token)
    ready: Book | None = None
    chunks: list = []
    try:
        async with _ingest_slot():   # other uploads wait here; their status says "Queued"
            ready, chunks, kind = await _ingest_work(book_id, title, author, kind, data, ext)
    except Exception as e:
        ingest_upload.discard_book(book_id)
        if isinstance(e, ingest_upload.UploadError):
            _set_status(book_id, "error", str(e))
        else:  # never leak details to the client
            logging.getLogger(__name__).warning("upload ingest failed: %s", type(e).__name__)
            _set_status(book_id, "error", "The file could not be processed.")
    finally:
        _ingest_guard.release(token)
        if hash_key is not None:   # book.json is written by now (or the ingest failed): the disk check takes over
            _hash_claims.pop(hash_key, None)
    if ready is not None and kind == "textbook" and ext == ".pdf" and any(p.kind == "math" or p.inline_math
                                                                         for p in ready.paragraphs):
        _start_equations(book_id, data)
    if ready is not None and kind == "novel" and book_id not in _deleted:
        _start_series_early(book_id)   # stacks the volume within seconds, before indexing and the queued enrichment
    if ready is not None and settings.retrieval_backend == "vertex":
        await _index_in_background(ready, chunks)
    if ready is not None and settings.upload_enrich and book_id not in _deleted:
        current = books.load_book(book_id)   # the reader may have moved it to the other shelf (or deleted it) meanwhile
        if current is None or current.kind != kind:
            return
        try:
            await _plan_enrichment(book_id, current)
        except Exception as e:  # the book stays readable with the narrator
            logging.getLogger(__name__).warning("enrichment planning failed: %s", type(e).__name__)
            _set_enrich(book_id, status="error",
                        message=enrich_pipeline.TEXTBOOK_FAILED if kind == "textbook" else enrich_pipeline.FAILED)


_series_tasks: set[asyncio.Task] = set()


def _start_series_early(book_id: str) -> None:
    from .enrich import series_early
    task = asyncio.create_task(series_early.detect_default(book_id))
    _series_tasks.add(task)
    task.add_done_callback(_series_tasks.discard)


async def _ingest_work(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str):
    """Parse, chunk and publish one upload (the caller holds an ingest slot). Returns (book, chunks, final kind)."""
    if book_id in _deleted:
        raise ingest_upload.UploadError("The book was deleted.")
    _set_status(book_id, "processing", "Reading the PDF" if ext == ".pdf" else "Reading the text")
    raw, detected = None, None
    if kind == "auto":  # decide novel or textbook from the extracted text
        raw = await asyncio.to_thread(ingest_upload.extract_text, data, ext, ingest_upload.figures_dir(book_id))
        kind, confidence, method = await detect_kind(ingest_upload.plain_text(raw), book_id=book_id)
        detected = {"kind": kind, "confidence": confidence, "method": method}
    book, chunks = await asyncio.to_thread(ingest_upload.process_upload, book_id, title, author, kind, data, ext,
                                           raw, detected)
    await asyncio.to_thread(ingest_upload.publish_book, book)  # readable now; Vertex indexing follows
    _index_for.cache_clear()
    _set_status(book_id, "ready", **({"kind": kind, "kind_detected": detected} if detected else {}))
    if settings.retrieval_backend == "vertex":
        _set_index(book_id, state="queued", done=0, total=len(chunks))
    return book, chunks, kind


_equation_jobs: set[str] = set()   # textbooks whose equation pictures / LaTeX are being made
_equation_tasks: set[asyncio.Task] = set()


def _start_equations(book_id: str, data: bytes) -> None:
    """Crop the published textbook's equations, then (Gemini configured) read them as LaTeX, in the background."""
    if book_id in _equation_jobs:
        return
    _equation_jobs.add(book_id)
    task = asyncio.create_task(_equations_job(book_id, data))
    _equation_tasks.add(task)
    task.add_done_callback(_equation_tasks.discard)


async def _equations_job(book_id: str, data: bytes) -> None:
    """Pictures first (the reader shows them at once), then LaTeX and spoken readings. Never raises; the book stays
    readable with whatever was saved."""
    stop = lambda: book_id in _deleted  # noqa: E731
    try:
        crops = await asyncio.to_thread(ingest_upload.render_equations, book_id, data, stop)
        inline, shared = await asyncio.to_thread(ingest_upload.render_inline, book_id, data, stop)
        if not (crops or inline) or stop():
            return
        if not enrich_equations.available():
            if settings.upload_enrich and public_client.active():   # a clone with only a Boson key
                await _public_equations(book_id, crops, inline, shared, stop)
            return
        llm = enrich_equations.make_llm("gemini")
        readings = await enrich_equations.transcribe(book_id, crops, llm=llm) if crops else {}
        if readings and not stop():
            await asyncio.to_thread(ingest_upload.update_equations, book_id, readings)
        readings = await enrich_equations.transcribe(book_id, inline, llm=llm, fit=True) if inline else {}
        fields = {i: r for k, r in readings.items() for i in shared.get(k, [k])}
        if fields and not stop():
            await asyncio.to_thread(ingest_upload.update_equations, book_id, fields)
    except Exception as e:
        logging.getLogger(__name__).warning("equations of %s failed: %s", book_id, type(e).__name__)
    finally:
        _equation_jobs.discard(book_id)


async def _public_equations(book_id: str, crops: dict, inline: dict, shared: dict, stop) -> None:
    """The equation readings through the public processing service (its caps); what it cannot read stays pictures."""
    display, inl, note = await public_client.transcribe_equations(book_id, crops, inline, stop=stop)
    if display and not stop():
        await asyncio.to_thread(ingest_upload.update_equations, book_id, display)
    fields = {i: r for k, r in inl.items() for i in shared.get(k, [k])}
    if fields and not stop():
        await asyncio.to_thread(ingest_upload.update_equations, book_id, fields)
    if note:
        logging.getLogger(__name__).info("equations of %s: %s", book_id, note)


_EQ_ID = re.compile(r"^(?:e\d{1,5}|m\d{1,6})$")  # display equation / inline formula


@app.get("/api/books/{book_id}/equations/{eq_id}.png")
def equation_picture(book_id: str, eq_id: str):
    """A cropped equation picture. Not behind the access code (an <img> cannot send it), like /portraits: a few
    formulas, never the text; PER_USER still serves only the books the caller may see."""
    if not books._ID.match(book_id) or not _EQ_ID.match(eq_id):
        raise HTTPException(404, "Not found")
    users.guard_book(book_id)
    path = ingest_upload.equation_path(book_id, eq_id)
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/books/upload", status_code=202, dependencies=[Depends(require_access)])
async def upload_book(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      title: str = Form(..., min_length=1, max_length=120),
                      author: str = Form("", max_length=120),
                      kind: Literal["auto", "novel", "textbook"] = Form("auto")):
    if not upload_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many uploads; wait a moment.")
    token = _ingest_guard.acquire()  # no await between the check and the hold
    if token is None:
        raise HTTPException(429, f"{UPLOAD_QUEUE_MAX} books are already being processed; try again shortly.")
    try:
        title = title.strip()
        if not title:
            raise HTTPException(422, "Title is required.")
        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".pdf", ".txt"):
            raise HTTPException(400, "Upload a .pdf or .txt file.")
        data = await file.read(ingest_upload.MAX_UPLOAD_BYTES + 1)  # bounded read (size is also checked up front by middleware)
        if len(data) > ingest_upload.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File is too large (20 MB maximum).")
        if ext == ".pdf" and not data.startswith(b"%PDF"):
            raise HTTPException(400, "That does not look like a PDF.")
        ident = users.current()
        digest = await asyncio.to_thread(ingest_upload.content_hash, data)
        hash_key = (ident.ns, digest)
        if hash_key in _hash_claims:   # the same file is being added right now
            _ingest_guard.release(token)
            return _duplicate("processing", _hash_claims[hash_key])
        _hash_claims[hash_key] = ""   # claimed before the lookup (no await since the check): a twin request 409s above
        try:
            found = await asyncio.to_thread(ingest_upload.find_upload, digest, ident)
            conflict = _upload_conflict(found)
            if conflict:
                _hash_claims.pop(hash_key, None)
                _ingest_guard.release(token)
                return conflict
            book_id, _ = ingest_upload.make_book_dir(title)
            _hash_claims[hash_key] = book_id
            users.claim_book(book_id)  # PER_USER: this upload belongs to the caller
            path = ingest_upload.upload_path(book_id, ext)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            await asyncio.to_thread(ingest_upload.record_upload_hash, digest, book_id, ident)
            _set_status(book_id, "processing", "Queued")
        except BaseException:
            _hash_claims.pop(hash_key, None)
            raise
        # releases the guard and the hash claim
        background.add_task(_ingest, book_id, title, author.strip(), kind, data, ext, token, hash_key)
    except BaseException:
        _ingest_guard.release(token)
        raise
    return {"book_id": book_id, "status": "processing"}


# (account ns, sha256 of the file) -> book id, from the upload request until its ingest ends (#82). After that the
# per-account upload_hashes.json and the book's folder say whether the file is already in the library.
_hash_claims: dict[tuple[str, str], str] = {}
STILL_DELETING = "This book is still being deleted — try again in a moment."


def _duplicate(reason: str, book_id: str, title: str | None = None) -> JSONResponse:
    """409 for a file the account already has: `detail` is the message, `book_id` the existing copy to open."""
    if reason == "deleting":
        detail = STILL_DELETING
    elif reason == "processing":
        detail = "This file is already being added to your library."
    else:
        detail = f"This file is already in your library as “{title}”." if title else "This file is already in your library."
    body = {"detail": detail, "reason": reason, "book_id": book_id} | ({"title": title} if title else {})
    return JSONResponse(body, status_code=409)


def _upload_conflict(found: tuple[str, str, str | None] | None) -> JSONResponse | None:
    """The 409 for an existing copy of the uploaded file, or None when it may be added. A tombstone left by a delete
    that is not running any more (a restart, a failed delete) does not block: the new upload is a new book."""
    if found is None:
        return None
    book_id, state, title = found
    running = book_id in _deleting or (_delete_status.get(book_id) or {}).get("status") == "deleting"
    if running:
        return _duplicate("deleting", book_id)
    return _duplicate("duplicate", book_id, title) if state == "ready" else None


describe_bucket = TokenBucket(capacity=12, per_seconds=60)  # per IP / account: each may cost one Gemini Flash call


@app.post("/api/books/describe", dependencies=[Depends(require_access)])
async def describe_upload(request: Request, file: UploadFile = File(...)):
    """Title and author for the Add book form, read from the chosen file (#83). Nothing is stored.

    May cost one Gemini Flash call, so: signed-in accounts only under PER_USER, a per-caller bucket here, and a global
    bucket, daily cap and budget reservation in describe_book. Only the start of a .txt is read (a PDF needs its whole
    structure, up to the upload cap); parsing runs under a small global limit (429 when full) with a timeout.
    """
    users.require_account("Sign in to add books.")
    if not describe_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many files at once; type the title for now.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".txt"):
        raise HTTPException(400, "Upload a .pdf or .txt file.")
    if ext == ".txt":
        data = await file.read(describe_book.TEXT_PREFIX_BYTES)
    else:
        data = await file.read(ingest_upload.MAX_UPLOAD_BYTES + 1)
        if len(data) > ingest_upload.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File is too large (20 MB maximum).")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "That does not look like a PDF.")
    try:
        return await describe_book.describe(data, ext)
    except describe_book.Busy:
        raise HTTPException(429, "Busy reading other files; type the title for now.") from None


@app.get("/api/books/{book_id}/status")
def book_status(book_id: str):
    users.guard_book(book_id)
    if book_id in _delete_status:   # a delete's background part is running or failed
        return _delete_status[book_id]
    st = _upload_status.get(book_id)
    if st is None and books._ID.match(book_id):
        st = ingest_upload.read_status(book_id)
        if st and st.get("status") == "processing":  # not in memory: the server restarted mid-ingest
            st = {"status": "error", "message": "Processing was interrupted; please upload again."}
        elif st and (st.get("enrich") or {}).get("status") in ("running", "queued"):  # restarted mid-enrichment
            st = {"status": "ready", "enrich": {"status": "error", "stage": st["enrich"].get("stage"),
                  "message": enrich_pipeline.INTERRUPTED}} | ({"enrich_retries": st["enrich_retries"]} if "enrich_retries" in st else {})
        if st and (st.get("index") or {}).get("state") in ("running", "queued"):  # restarted mid-indexing: BM25 keeps serving
            st = {**st, "index": {**st["index"], "state": "error", "message": "Search indexing was interrupted."}}
    if st is None:
        raise HTTPException(404, "Book not found")
    return st


@app.post("/api/books/{book_id}/enrich", status_code=202, dependencies=[Depends(require_access)])
async def start_enrichment(book_id: str, background: BackgroundTasks):
    """Start the character processing: after the reader confirmed it (testing mode, UPLOAD_CONFIRM), or again after it
    was interrupted, failed or stopped by a spending cap (status "error" / "refused"; only for an upload the caller owns;
    the caps are checked again, so a refused book only runs once they allow it)."""
    users.guard_book(book_id)
    st = book_status(book_id)
    state = (st.get("enrich") or {}).get("status")
    retry = state in ("error", "refused")   # refused: a cap stop; allowed again once the caps permit (re-checked below)
    if retry:
        users.require_account("Sign in to process books.")
        if not book_id.startswith("u-"):
            raise HTTPException(403, "Only books you uploaded can be processed again.")
    # check without an await before the new status is written: a second request then sees the job and gets a 409
    if book_id in _kind_claims or book_id in _enrich_jobs or book_id in _equation_jobs:   # a job or kind change is running
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    if state == "done":   # a stale "Try again" on a book that has finished meanwhile: just report it
        return st
    if state not in ("awaiting_confirmation", "error", "refused"):
        raise HTTPException(409, "This book is not waiting for confirmation.")
    if not settings.upload_enrich:   # UPLOAD_ENRICH=false: no paid processing, not even a retry
        raise HTTPException(409, ENRICH_OFF)
    retries = int(st.get("enrich_retries") or 0)
    if retry and retries >= MAX_ENRICH_RETRIES:
        raise HTTPException(409, TOO_MANY_TRIES)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    textbook = book.kind == "textbook"
    flag = {"summaries_only": True} if textbook else {}
    reason = enrich_pipeline.check_caps(enrich_pipeline.estimate_for(book), textbook=textbook, book_id=book_id)  # re-checked at start
    if reason:
        _set_enrich(book_id, status="refused", message=reason, **flag)
        return _upload_status[book_id]
    extra = {"enrich_retries": retries + 1} if retry else {}
    _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start", **flag, **extra)
    background.add_task(_run_enrichment, book_id, resume=retry)
    return _upload_status[book_id]


class KindBody(BaseModel):
    kind: Literal["novel", "textbook"]


async def _reread_as_textbook(book: Book) -> tuple[Book | None, bytes | None]:
    """(re-read text, PDF bytes) for an uploaded PDF moving to Textbooks, else (None, None). The expensive read runs
    in a thread and outside the book's write lock; a failure only logs its type."""
    pdf = ingest_upload.upload_path(book.id, ".pdf")
    if not pdf.is_file():
        return None, None
    try:
        data = await asyncio.to_thread(pdf.read_bytes)
        return await asyncio.to_thread(ingest_upload.rebuild_textbook_text, book, data), data
    except Exception as e:
        logging.getLogger(__name__).warning("textbook restore failed: %s", type(e).__name__)
        return None, None


async def _reindex_moved_book(book: Book, background: BackgroundTasks) -> None:
    """Retrieval index for a book whose text was re-read, as an upload builds it: the local chunks always, then (vertex)
    the old text's leftover datapoints removed and the new chunks embedded in the background."""
    def local():
        stale: list[str] = []
        if settings.retrieval_backend == "vertex":
            from .gcp.indexing import datapoint_ids
            stale = datapoint_ids(book)   # still the old chunks.json
        chunks = retrieval.make_chunks(book)
        retrieval.save_index(settings.data_dir / "books" / book.id / "index", chunks, None)
        keep = {f"{book.id}__{c.id}" for c in chunks}
        return chunks, [i for i in stale if i not in keep]
    chunks, stale = await asyncio.to_thread(local)
    _index_for.cache_clear()
    if settings.retrieval_backend == "vertex":
        background.add_task(_reindex_vertex, book, chunks, stale)


async def _reindex_vertex(book: Book, chunks, stale: list[str]) -> None:
    """Drop the old text's datapoints that the new chunks do not overwrite, then index the new chunks. Never raises."""
    if stale:
        try:
            await _vertex_deps()[1].delete(stale)
        except Exception as e:   # a leftover datapoint is still bound by its own para_end (spoiler rule holds)
            logging.getLogger(__name__).warning("vertex delete failed: %s", type(e).__name__)
    await _index_in_background(book, chunks)


@app.post("/api/books/{book_id}/kind", status_code=202, dependencies=[Depends(require_access)])
async def change_kind(book_id: str, body: KindBody, background: BackgroundTasks):
    """Move an uploaded book between the Novels and Textbooks shelves.

    textbook -> novel starts enrichment (same caps and UPLOAD_CONFIRM behaviour as an upload) unless the book already
    has its characters; novel -> textbook keeps the characters in the file (the UI ignores them) and writes the
    textbook recap notes (same caps) only when the book has no summaries at all. A PDF moved to Textbooks is read
    again so the display equations and inline formulas dropped while it was a novel come back (with their pictures);
    its old summaries go (their paragraph numbers belong to the old text), so the textbook notes are written.
    """
    users.guard_book(book_id)
    if not book_id.startswith("u-"):
        raise HTTPException(403, "Only books you uploaded can be moved.")
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    try:
        st = book_status(book_id)
    except HTTPException:  # an upload from before status files
        st = {"status": "ready"}
    # Check and claim in one synchronous block (no await before the claim): concurrent requests cannot both pass.
    if (st.get("enrich") or {}).get("status") == "running" or book_id in _enrich_jobs or book_id in _kind_claims \
            or book_id in _equation_jobs:
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    if book.kind == body.kind:
        return st
    _kind_claims.add(book_id)
    try:
        def set_kind(b) -> None:
            b.kind = body.kind
        path = settings.data_dir / "books" / book_id / "book.json"
        rebuilt, pdf_bytes = await _reread_as_textbook(book) if body.kind == "textbook" else (None, None)
        if rebuilt is not None:
            try:  # applied to a fresh read under the lock: a write made during the re-read is kept
                book = await asyncio.to_thread(books.update_book, path,
                                               lambda b: ingest_upload.apply_textbook_text(b, rebuilt))
            except Exception as e:  # still move the shelf; the text they already have stays
                logging.getLogger(__name__).warning("textbook restore failed: %s", type(e).__name__)
                rebuilt = pdf_bytes = None
        if rebuilt is None:
            # a fresh read under the book's write lock (never the cached object, never a stale copy of it)
            book = await asyncio.to_thread(books.update_book, path, set_kind)
        _index_for.cache_clear()
        _set_status(book_id, "ready", kind=body.kind)
        if rebuilt is not None:
            await _reindex_moved_book(book, background)
            if any(p.kind == "math" or p.inline_math for p in book.paragraphs):
                _start_equations(book_id, pdf_bytes)
        # a novel's own summaries are kept when it moves to Textbooks (recap then uses them as before)
        needs = not book.characters if body.kind == "novel" else not book.summaries
        if settings.upload_enrich and needs:
            if _decide_enrichment(book_id, book):  # sets status "running" (queued) before the claim is released
                background.add_task(_run_enrichment, book_id)
        return _upload_status[book_id]
    finally:
        _kind_claims.discard(book_id)


def _book_busy(book_id: str) -> bool:
    """True while an ingest, Vertex indexing (running or queued), enrichment or kind change for this book is running."""
    st = _upload_status.get(book_id) or {}
    return (st.get("status") == "processing" or (st.get("enrich") or {}).get("status") == "running"
            or (st.get("index") or {}).get("state") in ("running", "queued") or book_id in _index_jobs
            or book_id in _enrich_jobs or book_id in _kind_claims)


def _forget_book(book_id: str) -> None:
    """Drop every in-memory trace of a book (library listing, serialised bodies, indexes, status)."""
    _upload_status.pop(book_id, None)
    _book_bodies.pop(book_id, None)
    for k in [k for k in books._cache if Path(k).parent.name == book_id]:
        books._cache.pop(k, None)
    books._series_index = None
    _index_for.cache_clear()


@app.delete("/api/books/{book_id}", status_code=202, dependencies=[Depends(require_access)])
async def delete_book(book_id: str, background: BackgroundTasks):
    """Really remove an uploaded book: its Vertex datapoints, folder, original file and caches. Owner only.

    Fast path in the request: book.json goes (the book leaves the library at once) and the Vertex datapoint ids are
    saved to deleting.json. The slow part (one Vertex DELETE per chunk, ~1-2 min for a 1300-chunk textbook) runs in
    the background with its state on /status. Idempotent: a retry joins a running delete or resumes a failed one.
    Book ids are never reused, so a re-upload of the same file is a new book and cannot be touched by this.
    """
    if not books._ID.match(book_id):
        raise HTTPException(404, "Book not found")
    users.guard_book(book_id)   # 404 for another account's book
    ident = users.require_account()   # PER_USER: signed in only, never the shared anonymous namespace
    if not book_id.startswith("u-"):
        raise HTTPException(403, "Only books you uploaded can be deleted.")
    if ident.ns and users.book_owner_ns(book_id) != ident.ns:   # explicit owner check, whatever can_see_book allows
        raise HTTPException(404, "Book not found")
    accepted = JSONResponse({"deleted": book_id, "status": "deleting"}, status_code=202)
    if book_id in _deleting:   # a retry while the background part runs: join it, do not 409 or delete twice
        return accepted
    book_dir = settings.data_dir / "books" / book_id
    if not book_dir.is_dir():
        raise HTTPException(404, "Book not found")
    # A resume needs the persisted tombstone (or this process's record of a failed delete). A folder without book.json
    # and without a tombstone is an upload still being ingested: never delete that from under its job.
    resuming = (book_dir / "deleting.json").is_file() or book_id in _delete_status
    if _book_busy(book_id) or not (resuming or (book_dir / "book.json").is_file()):
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    # Claim synchronously (no await since the checks): blocks a concurrent kind change / enrichment / second delete.
    _kind_claims.add(book_id)
    _deleting[book_id] = time.monotonic()
    try:
        ids = await asyncio.to_thread(_tombstone_book, book_id)
    except BaseException:
        _kind_claims.discard(book_id)
        _deleting.pop(book_id, None)
        raise
    _deleted.add(book_id)   # from now on no background job writes this book back
    _forget_book(book_id)
    _delete_status[book_id] = {"status": "deleting", "message": "Removing the book"}
    background.add_task(_finish_delete, book_id, ids)
    return accepted


_deleting: dict[str, float] = {}       # book id -> start time, while a delete's background part runs
_delete_status: dict[str, dict] = {}   # "deleting" | "delete_error" while the folder still exists


def _tombstone_book(book_id: str) -> list[str]:
    """Worker thread: save the Vertex datapoint ids next to the book (deleting.json), then remove book.json so the
    book leaves the library. On a resume the ids come from deleting.json (book.json is already gone)."""
    d = settings.data_dir / "books" / book_id
    tomb = d / "deleting.json"
    ids: list[str] = []
    if tomb.is_file():
        try:
            ids = [i for i in json.loads(tomb.read_text(encoding="utf-8")) if isinstance(i, str)]
        except (OSError, ValueError):
            ids = []
    else:   # written first, for every backend: it is what marks a resumable delete
        if settings.retrieval_backend == "vertex":
            from .gcp.indexing import datapoint_ids
            book = books.load_book(book_id)
            ids = datapoint_ids(book) if book else []
        tmp = tomb.with_name(tomb.name + ".tmp")
        tmp.write_text(json.dumps(ids), encoding="utf-8")
        tmp.replace(tomb)
    for name in ("book.json", "book.json.tmp"):
        (d / name).unlink(missing_ok=True)
    return ids


async def _finish_delete(book_id: str, ids: list[str]) -> None:
    """Background: the Vertex datapoints first (on failure the folder and deleting.json stay, so a retry resumes),
    then every file. Never raises; releases the claim taken by delete_book."""
    log = logging.getLogger(__name__)
    try:
        if ids:
            try:
                await _vertex_deps()[1].delete(ids)
            except Exception as e:
                log.warning("vertex delete failed: %s", type(e).__name__)
                _delete_status[book_id] = {"status": "delete_error",
                                           "message": "Could not remove the book from search; try deleting again."}
                return
        left = await asyncio.to_thread(ingest_upload.delete_book_files, book_id)
        _forget_book(book_id)
        if left:
            log.warning("book %s: could not remove %s", book_id, left)
            _delete_status[book_id] = {"status": "delete_error", "message": "Some files could not be removed; try deleting again."}
        else:
            _delete_status.pop(book_id, None)
    except Exception as e:  # never leave the claim behind
        log.warning("book delete failed: %s", type(e).__name__)
        _delete_status[book_id] = {"status": "delete_error", "message": "The delete did not finish; try again."}
    finally:
        _kind_claims.discard(book_id)
        _deleting.pop(book_id, None)


# ---------- avatars (stretch) ----------

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _avatar_dir(book_id: str) -> Path:
    return settings.data_dir / "cache" / "avatar" / book_id


def _guard_avatar_book(book_id: str) -> None:
    """Book clips live in cache/avatar/<book id>, next to the shared lip-sync folder "talk". With PER_USER only real,
    visible books get that far, so no book id can reach another account's clips through a reserved folder name."""
    users.guard_book(book_id)
    if settings.per_user and (book_id in ("talk", "books") or books.load_book(book_id) is None):
        raise HTTPException(404, "Book not found")


@app.get("/api/avatars/{book_id}")
def list_avatars(book_id: str):
    """Chunk ids that have a pre-rendered avatar clip, so the reader only asks for clips that exist."""
    if not _SAFE_NAME.match(book_id):
        raise HTTPException(404, "Book not found")
    _guard_avatar_book(book_id)
    d = _avatar_dir(book_id)
    return sorted(p.stem for p in d.glob("*.mp4")) if d.is_dir() else []


_TTS_URL = re.compile(r"^/media/tts/([0-9a-f]{64})\.mp3$")
avatar_bucket = TokenBucket(capacity=12, per_seconds=60)  # every render spends Boson avatar credit
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
    return audio, face, f"{m.group(1)}-{hashlib.sha256(face.read_bytes()).hexdigest()[:16]}"


@app.post("/api/avatar/talk", dependencies=[Depends(require_access)])
async def lipsync(req: LipSyncRequest, request: Request):
    """Lip-synced clip of one answer sentence: the voice's face speaking that cached TTS audio (~10 s to render).

    Only audio this server synthesised (/media/tts/<key>.mp3) and only voices with a face are accepted; clips are
    cached by audio key, so the same sentence never renders twice.
    """
    audio, face, key = _lipsync_inputs(req)
    clip = _lipsync_dir() / f"{key}.mp4"
    await _acquire_avatar_key(key)
    try:
        if not clip.is_file():
            if not avatar_bucket.allow(rate_key(request)):
                raise HTTPException(429, "Too many lip-sync renders; wait a moment.")
            data = audio.read_bytes()
            try:
                vid = await boson.create_video(base64.b64encode(face.read_bytes()).decode(), base64.b64encode(data).decode(),
                                               image_mime=faces.FACE_EXTS[face.suffix.lstrip(".").lower()],
                                               seconds=mp3_seconds(data))
                await boson.wait_video(vid, interval=1, timeout=120)
                video = await boson.download_video(vid)
            except (BosonError, RuntimeError) as e:
                raise boson_http_error(e)
            clip.parent.mkdir(parents=True, exist_ok=True)
            tmp = clip.with_name(f"{clip.stem}.{secrets.token_hex(6)}.tmp")
            tmp.write_bytes(video)
            tmp.replace(clip)
    finally:
        _release_avatar_key(key)
    return {"video_url": f"/media/avatar/{clip.name}"}


STREAM_MAX_BYTES = 40 * 1024 * 1024
STREAM_MAX_SECONDS = 150


@app.post("/api/avatar/stream", dependencies=[Depends(require_access)])
async def lipsync_stream(req: LipSyncRequest, request: Request):
    """Streaming lip-sync: proxies Boson's fragmented-MP4 stream so playback starts at the first frames.

    A clip that is already cached answers with JSON {"video_url"} instead (the client plays that file). Otherwise the
    bytes are forwarded as they arrive and teed to a temp file that joins the cache only if the stream completes.
    """
    audio, face, key = _lipsync_inputs(req)
    clip = _lipsync_dir() / f"{key}.mp4"
    if clip.is_file():
        return {"video_url": f"/media/avatar/{clip.name}"}
    await _acquire_avatar_key(key)
    handed_off = False  # the response body owns the lock from the moment it is returned
    stack = AsyncExitStack()
    try:
        if clip.is_file():  # another request finished it while we waited
            return {"video_url": f"/media/avatar/{clip.name}"}
        if not avatar_bucket.allow(rate_key(request)):
            raise HTTPException(429, "Too many lip-sync renders; wait a moment.")
        data = audio.read_bytes()
        started = time.monotonic()
        try:
            async with asyncio.timeout(STREAM_MAX_SECONDS):
                _vid, chunks = await stack.enter_async_context(boson.stream_video(
                    base64.b64encode(face.read_bytes()).decode(), base64.b64encode(data).decode(),
                    image_mime=faces.FACE_EXTS[face.suffix.lstrip(".").lower()], seconds=mp3_seconds(data)))
        except (BosonError, RuntimeError) as e:
            raise boson_http_error(e)
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


@app.get("/api/avatar/{chunk_id}")
def avatar(chunk_id: str, book: str):
    if not (_SAFE_NAME.match(chunk_id) and _SAFE_NAME.match(book)):
        raise HTTPException(404, "No avatar clip")
    _guard_avatar_book(book)
    path = _avatar_dir(book) / f"{chunk_id}.mp4"
    if not path.is_file():
        raise HTTPException(404, "No avatar clip for this line")
    return FileResponse(path, media_type="video/mp4")


# ---------- voice cloning ----------

@app.post("/api/voices/clone", dependencies=[Depends(require_access)])
async def clone_voice(request: Request, audio: UploadFile = File(...), transcript: str = Form(..., max_length=2000),
                      name: str = Form(..., max_length=60), consent: str = Form("false"),
                      face: UploadFile | None = File(None), face_id: str | None = Form(None, max_length=80)):
    """Clone a voice. `face` (an image) stores a new face bound to it; `face_id` (#113) links an existing face of the
    caller's library instead, with no copy: deleting the voice later only drops that link."""
    if not clone_bucket.allow(rate_key(request)):
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
        voice_id = await boson.create_voice(base64.b64encode(data).decode(), transcript, description=name)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
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


@app.delete("/api/voices/{voice_id}", dependencies=[Depends(require_access)])
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
        await boson.delete_voice(voice_id)   # False (404) = already gone at Boson: carry on
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)            # local data stays, so the delete can be retried
    face_ids = await asyncio.to_thread(faces.unlink_voice, voice_id)   # faces stay; they only lose the voice link
    await asyncio.to_thread(faces.delete_legacy_voice_files, voice_id)
    voices.remove_voice(voice_id)
    return {"deleted": voice_id, "faces": face_ids}


@app.get("/api/voices", dependencies=[Depends(require_access)])
def list_voices():
    return voices.load_voices()


@app.get("/api/voices/faces", dependencies=[Depends(require_access)])
def list_voice_faces():
    """voice id -> portrait URL for cloned voices that have a face (shown for any speaker using that voice).
    Gated: the face is a real person's. The files are named after the voice id, which only these gated lists reveal."""
    return faces.voice_faces()


# ---------- face library ----------

face_bucket = TokenBucket(capacity=10, per_seconds=60)  # uploads write to disk
_face_locks: dict[str, asyncio.Lock] = {}


def _safe_face_id(face_id: str) -> str:
    if not faces.FACE_ID.match(face_id):
        raise HTTPException(404, "Face not found")
    return face_id


@app.post("/api/faces", dependencies=[Depends(require_access)])
async def upload_face(request: Request, image: UploadFile = File(...), name: str = Form(..., max_length=60)):
    if not face_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many face uploads; wait a moment.")
    data = await image.read(faces.MAX_FACE_BYTES + 1)  # bounded read
    try:
        return faces.create_face(data, name)
    except faces.FaceInputError as e:
        raise HTTPException(400, str(e))


face_detect_bucket = TokenBucket(capacity=12, per_seconds=60)  # per IP / account: each may cost one Gemini Flash call


@app.post("/api/faces/detect", dependencies=[Depends(require_access)])
async def detect_face(request: Request, image: UploadFile = File(...)):
    """The main face's box ({x, y, w, h} as fractions, or null) for the head-and-shoulders crop (#109). Nothing is
    stored. Guarded like /api/books/describe: accounts only under PER_USER, a per-caller bucket here, and a global
    bucket, daily cap and budget reservation in face_detect."""
    users.require_account("Sign in to add faces.")
    if not face_detect_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many photos at once; place the square yourself.")
    data = await image.read(faces.MAX_FACE_BYTES + 1)  # bounded read
    try:
        ext = faces.validate_image(data)
    except faces.FaceInputError as e:
        raise HTTPException(400, str(e))
    return {"face": await face_detect.detect(data, ext)}


@app.get("/api/faces", dependencies=[Depends(require_access)])
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


@app.delete("/api/faces/{face_id}", dependencies=[Depends(require_access)])
async def delete_face(face_id: str):
    users.require_account()
    _safe_face_id(face_id)
    if not await _delete_face_locked(face_id):
        raise HTTPException(404, "Face not found")
    return {"deleted": face_id}


def _release_face_lock(face_id: str, lock: asyncio.Lock) -> None:
    if not lock.locked() and _face_locks.get(face_id) is lock:
        del _face_locks[face_id]


@app.post("/api/faces/{face_id}/animate", dependencies=[Depends(require_access)])
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
                if not avatar_bucket.allow(rate_key(request)):
                    raise HTTPException(429, "Too many avatar renders; wait a moment.")
                try:
                    face = await faces.animate(boson, face_id)
                except (BosonError, RuntimeError) as e:
                    raise boson_http_error(e)
                return {**face, "animated": True, "approx_cost_usd": faces.APPROX_ANIMATE_COST_USD}
            return {**faces.get_face(face_id), "animated": False, "approx_cost_usd": 0}
    finally:
        _release_face_lock(face_id, lock)


# ---------- cast settings per book ----------

def _book_for_cast(book_id: str) -> Book:
    book = books.load_book(book_id) if _SAFE_NAME.match(book_id) and users.can_see_book(book_id) else None
    if book is None:
        raise HTTPException(404, "Book not found")
    return book


@app.get("/api/books/{book_id}/cast", dependencies=[Depends(require_access)])
def get_cast(book_id: str):
    return cast.load(_book_for_cast(book_id))


@app.put("/api/books/{book_id}/cast", dependencies=[Depends(require_access)])
def put_cast(book_id: str, body: cast.CastSettings):
    try:
        return cast.save(_book_for_cast(book_id), body)
    except cast.CastError as e:
        raise HTTPException(400, str(e))


# ---------- single-service deploy: serve the built frontend from the same origin (no CORS, one container) ----------
_dist = settings.data_dir.parent / "frontend" / "dist"
if _dist.is_dir():  # registered last so every /api, /media and /portraits route wins
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


if settings.public_api:  # PUBLIC_API=true through any entry point: expose only the public processing API
    from .public_api import app  # noqa: E402,F811
