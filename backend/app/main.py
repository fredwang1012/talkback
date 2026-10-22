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

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from pydantic import BaseModel, Field

from . import books, cast, faces, ingest_upload, public_client, retrieval, tts_cache, users, voices
from .retrieval import DEFAULT_K, REALTIME_K, REALTIME_MAX_PASSAGE_CHARS
from .boson import BosonClient, BosonError
from .config import settings
from .enrich import pipeline as enrich_pipeline
from .enrich.detect import detect_kind
from .gcp import usage
from .personas import build_answer_prompt, build_instructions
from .ratelimit import TokenBucket, client_ip
from .schema import Book
from .uploadlimit import UploadSizeLimit

def _warm_up() -> None:
    """Load the embedding model and every book's index before the first question (the model takes ~17 s to load)."""
    try:
        for info in books.list_books():
            _index_for(info["id"])
        if settings.retrieval_backend == "vertex":  # first Vertex call pays auth + TLS setup (~5 s): pay it now
            _vertex_deps()[0].embed_query("warm up")
    except Exception:  # never block startup; retrieval falls back to BM25 / loads lazily
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(users.migrate_on_startup)  # PER_USER only; never raises
    task = asyncio.create_task(asyncio.to_thread(_warm_up))
    yield
    task.cancel()


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


_book_bodies: dict[str, tuple[Book, bytes, bytes]] = {}  # book id -> (book object, json, gzipped json)


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
def get_recap(book_id: str, para: int = 0):
    """Spoiler-safe "Previously..." for the reader's place: the latest stored summary written from text at or
    before `para` (never a later one), the chapter they are in, and the closing summaries of earlier books."""
    users.guard_book(book_id)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    _, para = retrieval._clamp(book, para)
    ok = [s for s in book.summaries if s.upto_para <= para]
    best = max(ok, key=lambda s: s.upto_para) if ok else None
    sections = [s for s in book.sections if s.start_para <= para]
    chapter = max(sections, key=lambda s: s.start_para).title if sections else ""
    return {"upto_para": best.upto_para if best else None, "chapter": chapter,
            "text": best.text if best else "", "earlier": books.series_recap(book)}


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
                cached = tts_cache.is_cached(c.tts_text, voice)
                if req.cached_only and not cached:
                    raise HTTPException(409, "This line is not pre-rendered yet. Run preprocess/prerender_tts.py before the demo.")
                if not cached and not tts_bucket.allow(rate_key(request)):
                    raise HTTPException(429, "Too many new audio requests; wait a moment.")
                try:
                    out = await tts_cache.get_or_create(boson, c.tts_text, voice, p.text[c.char_start:c.char_end])
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
    # realtime (Boson) instructions must stay short: fewer passages and a smaller character cap than /api/answer
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, min(req.k, REALTIME_K),
                                    spoilers=req.spoilers, max_chars=REALTIME_MAX_PASSAGE_CHARS, **_series_context(book))
    out["instructions"] = build_instructions(book, req.speaker_id, out["story_so_far"], out["passages"], spoilers=req.spoilers)
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
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, req.k, spoilers=req.spoilers,
                                    **_series_context(book))
    t_retrieved = time.monotonic()
    retrieve_ms = int((t_retrieved - t0) * 1000)
    if _public_answers():  # passages above are already spoiler-filtered here; the service only writes the answer
        return StreamingResponse(_public_answer_stream(book, speaker, req, out, retrieve_ms, t_retrieved),
                                 media_type="application/x-ndjson")
    system, user = build_answer_prompt(book, req.speaker_id, out["story_so_far"], out["passages"], req.query,
                                        spoilers=req.spoilers)

    async def stream():
        first_ms = None
        try:
            async for sentence in get_answerer().stream_sentences(system, user):
                if first_ms is None:
                    first_ms = int((time.monotonic() - t_retrieved) * 1000)
                yield _line({"type": "sentence", "text": sentence})
        except Exception as e:  # never leak stack traces or secrets to the client
            logging.getLogger(__name__).warning("answer stream failed: %s", type(e).__name__)
            yield _line({"type": "error", "message": "The answer could not be generated; please try again."})
            return
        yield _line({"type": "done", "passages": len(out["passages"]), "retrieve_ms": retrieve_ms,
                     "first_sentence_ms": first_ms if first_ms is not None else 0})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def _public_answer_stream(book: Book, speaker, req: RetrieveRequest, out: dict, retrieve_ms: int, t_retrieved: float):
    first_ms = None
    payload = public_client.answer_payload(book, speaker, req.speaker_id, out, req.query, req.spoilers)
    try:
        async for msg in public_client.stream_answer(payload):
            if first_ms is None:
                first_ms = int((time.monotonic() - t_retrieved) * 1000)
            yield _line({"type": "sentence", "text": msg["text"]})
    except public_client.PublicFailure as e:
        logging.getLogger(__name__).warning("public answer failed: %s", e.kind)
        if e.kind != "limit":
            public_client.mark_unavailable()  # health then reports "realtime" and the page answers through Realtime
        yield _line({"type": "error", "message": public_client.ANSWER_LIMIT if e.kind == "limit"
                     else public_client.ANSWER_UNAVAILABLE})
        return
    yield _line({"type": "done", "passages": len(out["passages"]), "retrieve_ms": retrieve_ms,
                 "first_sentence_ms": first_ms if first_ms is not None else 0})


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: str


@app.post("/api/speak", dependencies=[Depends(require_access)])
async def speak(req: SpeakRequest, request: Request):
    if not voices.is_known_voice(req.voice):
        raise HTTPException(400, f"Unknown voice '{req.voice}'.")
    if not tts_cache.is_cached(req.text, req.voice) and not tts_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many new audio requests; wait a moment.")
    try:
        out = await tts_cache.get_or_create(boson, req.text, req.voice, req.text)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
    return {"voice": req.voice, **out}


# ---------- user uploads (PDF / .txt -> narrator-only book) ----------

_upload_status: dict[str, dict] = {}
upload_bucket = TokenBucket(capacity=3, per_seconds=60)  # uploads per IP


class IngestGuard:
    """Allows one ingest at a time (a 20 MB PDF is a lot of CPU and memory).

    A hold older than STALE_SECONDS may be taken over only if its background task never started (it is dead).
    A task that started and has not released is still running (a thread cannot be killed), so it keeps the guard
    and new uploads keep getting 429. Release only works for the current holder's token.
    """
    STALE_SECONDS = 15 * 60

    def __init__(self, clock=time.monotonic):
        self.clock, self._since, self._token = clock, None, 0
        self._started = self._warned = False

    @property
    def busy(self) -> bool:
        return self._since is not None

    def acquire(self) -> int | None:
        if self._since is not None:
            if self.clock() - self._since < self.STALE_SECONDS:
                return None
            log = logging.getLogger(__name__)
            if self._started:
                if not self._warned:
                    self._warned = True
                    log.warning("an ingest has been running for over 15 minutes; new uploads stay blocked")
                return None
            log.warning("ingest guard was held for over 15 minutes by a task that never started; taking it over")
        self._token += 1
        self._since = self.clock()
        self._started = self._warned = False
        return self._token

    def mark_started(self, token: int) -> None:
        if token == self._token:
            self._started = True

    def release(self, token: int) -> None:
        if token == self._token:
            self._since = None


_ingest_guard = IngestGuard()


def _set_status(book_id: str, status: str, message: str | None = None, **kind_info) -> None:
    _upload_status[book_id] = ingest_upload.write_status(book_id, status, message, **kind_info)


def _set_index(book_id: str, **fields) -> None:
    """Record the Vertex indexing progress {state, done, total, message?} next to the (ready) status."""
    _upload_status[book_id] = ingest_upload.write_status(book_id, "ready", index=fields)


async def _index_in_background(book: Book, chunks) -> None:
    """Index a published book into Vertex; the book is readable (BM25) meanwhile. Never raises."""
    book_id, total = book.id, len(chunks)
    try:
        _set_index(book_id, state="running", done=0, total=total)
        embedder, store = _vertex_deps()
        from .gcp.indexing import index_book
        await index_book(book, chunks, embedder, store,
                         on_progress=lambda done, tot: _set_index(book_id, state="running", done=done, total=tot))
        _set_index(book_id, state="done", done=total, total=total)
    except Exception as e:  # the book stays readable and keeps the BM25 fallback
        logging.getLogger(__name__).warning("vertex indexing failed: %s", type(e).__name__)
        _set_index(book_id, state="error", done=0, total=total, message="Search indexing failed; using basic search.")
    finally:
        _index_for.cache_clear()


def _set_enrich(book_id: str, **fields) -> None:
    """Record the novel enrichment sub-status next to the (ready) ingest status."""
    enrich = {k: v for k, v in fields.items() if v is not None}
    _upload_status[book_id] = ingest_upload.write_status(book_id, "ready", enrich=enrich)


_enrich_lock = asyncio.Lock()  # one enrichment at a time (cost control and rate limits)


async def _enrich_indexer(book: Book) -> None:
    """Final stage: refresh the Vertex index with the speakers (the local index needs no change)."""
    if settings.retrieval_backend == "vertex":
        from .gcp.indexing import index_book, load_chunks
        embedder, store = _vertex_deps()
        await index_book(book, await asyncio.to_thread(load_chunks, book), embedder, store)


_enrich_jobs: set[str] = set()   # books with an enrichment job queued or running
_kind_claims: set[str] = set()   # books whose kind change is being handled (check-and-claim, see change_kind)


async def _run_enrichment(book_id: str) -> None:
    if book_id in _enrich_jobs:  # defence in depth: never two paid jobs for one book
        return
    _enrich_jobs.add(book_id)
    try:
        _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start")
        async with _enrich_lock:
            book = books.load_book(book_id)
            if book is not None and book.characters:  # already enriched: nothing to pay for
                _set_enrich(book_id, status="done", stage="done", progress=1.0,
                            message="Characters, recap and search are ready.")
                return
            run = public_client.enrich_book if public_client.active() else enrich_pipeline.enrich_book
            await run(book_id, lambda **f: _set_enrich(book_id, **f), indexer=_enrich_indexer)
    finally:
        _enrich_jobs.discard(book_id)
        _index_for.cache_clear()


def _decide_enrichment(book_id: str, book: Book) -> bool:
    """Refuse (caps), wait for confirmation (testing mode) or mark as queued. True when the job should now run."""
    est = enrich_pipeline.estimate(book)
    reason = enrich_pipeline.check_caps(est)
    if reason:
        _set_enrich(book_id, status="refused", message=reason)
    elif settings.upload_confirm:
        _set_enrich(book_id, status="awaiting_confirmation", estimate_usd=round(est, 2),
                    message="Characters, voices and recap need processing.")
    else:
        _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start")
        return True
    return False


async def _plan_enrichment(book_id: str, book: Book) -> None:
    """After the text ingest of a novel: refuse (caps), wait for confirmation (testing mode) or start."""
    if _decide_enrichment(book_id, book):
        await _run_enrichment(book_id)


async def _ingest(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str, token: int) -> None:
    """Background ingest: CPU work in a thread, then publish book.json (readable, local BM25 retrieval), then (vertex
    only) index the chunks in the background with progress in status.json["index"].

    book.json is written after the CPU work, so a processing or failed upload is never listed or readable; a Vertex
    indexing failure keeps the book readable. Never raises. A novel is then enriched (cast, dialogue, summaries) while it is already readable with the narrator.
    """
    _ingest_guard.mark_started(token)
    ready: Book | None = None
    chunks: list = []
    try:
        raw, detected = None, None
        if kind == "auto":  # decide novel or textbook from the extracted text
            raw = await asyncio.to_thread(ingest_upload.extract_text, data, ext)
            kind, confidence, method = await detect_kind(raw, book_id=book_id)
            detected = {"kind": kind, "confidence": confidence, "method": method}
        book, chunks = await asyncio.to_thread(ingest_upload.process_upload, book_id, title, author, kind, data, ext,
                                               raw, detected)
        await asyncio.to_thread(ingest_upload.publish_book, book)  # readable now; Vertex indexing follows
        _index_for.cache_clear()
        _set_status(book_id, "ready", **({"kind": kind, "kind_detected": detected} if detected else {}))
        if settings.retrieval_backend == "vertex":
            _set_index(book_id, state="running", done=0, total=len(chunks))
        ready = book
    except Exception as e:
        ingest_upload.discard_book(book_id)
        if isinstance(e, ingest_upload.UploadError):
            _set_status(book_id, "error", str(e))
        else:  # never leak details to the client
            logging.getLogger(__name__).warning("upload ingest failed: %s", type(e).__name__)
            _set_status(book_id, "error", "The file could not be processed.")
    finally:
        _ingest_guard.release(token)
    if ready is not None and settings.retrieval_backend == "vertex":
        await _index_in_background(ready, chunks)
    if ready is not None and kind == "novel" and settings.upload_enrich:
        try:
            await _plan_enrichment(book_id, ready)
        except Exception as e:  # the book stays readable with the narrator
            logging.getLogger(__name__).warning("enrichment planning failed: %s", type(e).__name__)
            _set_enrich(book_id, status="error", message=enrich_pipeline.FAILED)


@app.post("/api/books/upload", status_code=202, dependencies=[Depends(require_access)])
async def upload_book(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      title: str = Form(..., min_length=1, max_length=120),
                      author: str = Form("", max_length=120),
                      kind: Literal["auto", "novel", "textbook"] = Form("auto")):
    if not upload_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many uploads; wait a moment.")
    token = _ingest_guard.acquire()  # no await between the check and the hold
    if token is None:
        raise HTTPException(429, "Another book is still being processed; try again shortly.")
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
        book_id, _ = ingest_upload.make_book_dir(title)
        users.claim_book(book_id)  # PER_USER: this upload belongs to the caller
        path = ingest_upload.upload_path(book_id, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _set_status(book_id, "processing")
        background.add_task(_ingest, book_id, title, author.strip(), kind, data, ext, token)  # releases the guard
    except BaseException:
        _ingest_guard.release(token)
        raise
    return {"book_id": book_id, "status": "processing"}


@app.get("/api/books/{book_id}/status")
def book_status(book_id: str):
    users.guard_book(book_id)
    st = _upload_status.get(book_id)
    if st is None and books._ID.match(book_id):
        st = ingest_upload.read_status(book_id)
        if st and st.get("status") == "processing":  # not in memory: the server restarted mid-ingest
            st = {"status": "error", "message": "Processing was interrupted; please upload again."}
        elif st and (st.get("enrich") or {}).get("status") == "running":  # restarted mid-enrichment
            st = {"status": "ready", "enrich": {"status": "error", "stage": st["enrich"].get("stage"),
                  "message": "Character processing was interrupted; the book is still readable with the narrator."}}
        if st and (st.get("index") or {}).get("state") == "running":  # restarted mid-indexing: BM25 keeps serving
            st = {**st, "index": {**st["index"], "state": "error", "message": "Search indexing was interrupted."}}
    if st is None:
        raise HTTPException(404, "Book not found")
    return st


@app.post("/api/books/{book_id}/enrich", status_code=202, dependencies=[Depends(require_access)])
async def start_enrichment(book_id: str, background: BackgroundTasks):
    """Testing mode (UPLOAD_CONFIRM): start the character processing the reader just confirmed."""
    st = book_status(book_id)
    if (st.get("enrich") or {}).get("status") != "awaiting_confirmation":
        raise HTTPException(409, "This book is not waiting for confirmation.")
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    reason = enrich_pipeline.check_caps(enrich_pipeline.estimate(book))  # caps are re-checked at start
    if reason:
        _set_enrich(book_id, status="refused", message=reason)
        return _upload_status[book_id]
    _set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start")
    background.add_task(_run_enrichment, book_id)
    return _upload_status[book_id]


class KindBody(BaseModel):
    kind: Literal["novel", "textbook"]


@app.post("/api/books/{book_id}/kind", status_code=202, dependencies=[Depends(require_access)])
async def change_kind(book_id: str, body: KindBody, background: BackgroundTasks):
    """Move an uploaded book between the Novels and Textbooks shelves.

    textbook -> novel starts enrichment (same caps and UPLOAD_CONFIRM behaviour as an upload) unless the book already
    has its characters; novel -> textbook only changes the kind (the characters stay in the file, the UI ignores them).
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
    if (st.get("enrich") or {}).get("status") == "running" or book_id in _enrich_jobs or book_id in _kind_claims:
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    if book.kind == body.kind:
        return st
    _kind_claims.add(book_id)
    try:
        book = book.model_copy(deep=True)  # load_book's result is cached and must not be mutated
        book.kind = body.kind
        await asyncio.to_thread(books.save_book, settings.data_dir / "books" / book_id / "book.json", book)
        _index_for.cache_clear()
        _set_status(book_id, "ready", kind=body.kind)
        if body.kind == "novel" and settings.upload_enrich and not book.characters:
            if _decide_enrichment(book_id, book):  # sets status "running" (queued) before the claim is released
                background.add_task(_run_enrichment, book_id)
        return _upload_status[book_id]
    finally:
        _kind_claims.discard(book_id)


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


def _lipsync_inputs(req: LipSyncRequest) -> tuple[Path, Path, str]:
    """Validate a lip-sync request: (cached TTS file, face image, cache key). Raises 400/404."""
    m = _TTS_URL.match(req.audio_url)
    face = faces.voice_face_path(req.voice) if voices.is_known_voice(req.voice) else None
    if not m or face is None:
        raise HTTPException(400, "Lip-sync needs audio from /api/speak and a voice that has a face.")
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
                                               seconds=len(data) / 16000)  # 128 kbit/s mp3
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
                    image_mime=faces.FACE_EXTS[face.suffix.lstrip(".").lower()], seconds=len(data) / 16000))
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
                      face: UploadFile | None = File(None)):
    if not clone_bucket.allow(rate_key(request)):
        raise HTTPException(429, "Too many voice uploads; wait a moment.")
    data = await audio.read(voices.MAX_BYTES + 1)  # bounded read: never buffer an unbounded upload
    image = await face.read(faces.MAX_FACE_BYTES + 1) if face is not None else None
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
    face_url = faces.create_face(image, name, voice=voice_id)["image"] if image is not None else None
    return {"voice": voice_id, "name": name, "face": face_url}


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


@app.get("/api/faces", dependencies=[Depends(require_access)])
def list_faces():
    """Gated: faces can be real people's portraits."""
    return faces.list_faces()


@app.delete("/api/faces/{face_id}", dependencies=[Depends(require_access)])
async def delete_face(face_id: str):
    _safe_face_id(face_id)
    lock = _face_locks.setdefault(face_id, asyncio.Lock())
    try:
        async with lock:  # waits for a running animate, so no loops are written after the delete
            deleted = faces.delete_face(face_id)
    finally:
        _release_face_lock(face_id, lock)
    if not deleted:
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
