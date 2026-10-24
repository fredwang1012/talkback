import asyncio
import base64
import gzip
import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import books, ingest_upload, retrieval, tts_cache, voices
from .boson import BosonClient, BosonError
from .config import settings
from .gcp import usage
from .personas import build_answer_prompt, build_instructions
from .ratelimit import TokenBucket
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
    task = asyncio.create_task(asyncio.to_thread(_warm_up))
    yield
    task.cancel()


app = FastAPI(title="Talkback", lifespan=lifespan)
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
    return settings.data_dir / "cache" / "avatar" / "talk"


_lipsync_dir().mkdir(parents=True, exist_ok=True)
app.mount("/media/avatar", StaticFiles(directory=_lipsync_dir()), name="lipsync")
app.mount("/portraits", StaticFiles(directory=settings.data_dir / "portraits", check_dir=False), name="portraits")

boson = BosonClient()
token_bucket = TokenBucket(capacity=10, per_seconds=60)
tts_bucket = TokenBucket(capacity=120, per_seconds=60)   # only cache misses (real Boson calls) consume this
clone_bucket = TokenBucket(capacity=5, per_seconds=60)
answer_bucket = TokenBucket(capacity=20, per_seconds=60)  # every /api/answer call spends Gemini credit


retrieve_bucket = TokenBucket(capacity=30, per_seconds=60)  # vertex retrieval spends embedding + search credit


def client_ip(request: Request) -> str:
    """Peer address. With TRUST_PROXY (Cloud Run is the only ingress) use the right-most X-Forwarded-For entry:
    the front end appends the real client there, while anything to its left is client-supplied and spoofable."""
    if settings.trust_proxy:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


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


@app.get("/api/health")
def health():
    return {"ok": True, "boson_key_set": bool(settings.boson_api_key), "access_required": bool(settings.access_token),
            "answer_backend": settings.effective_answer_backend}


@app.get("/api/books")
def get_books():
    return books.list_books()


_book_bodies: dict[str, tuple[Book, bytes, bytes]] = {}  # book id -> (book object, json, gzipped json)


@app.get("/api/books/{book_id}", response_model=Book, dependencies=[Depends(require_access)])
def get_book(book_id: str, accept_encoding: str = Header(default="")):
    """The full book. Gated like the paid endpoints: a public deployment must not hand out a copyrighted text.
    Serialised and gzipped once per book version (a novel is several MB of JSON)."""
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


# ---------- realtime key ----------

@app.post("/api/realtime/token", dependencies=[Depends(require_access)])
async def realtime_token(request: Request):
    """Mint a fresh ephemeral Realtime key per call (never cached). The real API key never leaves the server."""
    if not token_bucket.allow(client_ip(request)):
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
    book = books.load_book(req.book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    for p in book.paragraphs:
        for c in p.tts_chunks:
            if c.id == req.chunk_id:
                voice = req.voice_override or c.voice
                if not voices.is_known_voice(voice):
                    raise HTTPException(400, f"Unknown voice '{voice}'.")
                cached = tts_cache.is_cached(c.tts_text, voice)
                if req.cached_only and not cached:
                    raise HTTPException(409, "This line is not pre-rendered yet. Run preprocess/prerender_tts.py before the demo.")
                if not cached and not tts_bucket.allow(client_ip(request)):
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
    k: int = Field(default=4, ge=1, le=8)


@lru_cache(maxsize=1)
def _vertex_deps():
    """Shared embedder + vector store, built on first use (no network at import or construction of the index)."""
    from .gcp.embed import GeminiEmbedder
    from .gcp.vectorstore import VectorStore
    return GeminiEmbedder(), VectorStore(settings.vs_collection_path)


@lru_cache(maxsize=8)
def _index_for(book_id: str) -> retrieval.Index:
    book = books.load_book(book_id)
    if settings.retrieval_backend == "vertex":
        from .gcp.vertex_index import VertexIndex
        earlier = {b.id: b.title for b in books.earlier_in_series(book)}
        return VertexIndex(book, _LazyEmbedder(), _LazyStore(), earlier)
    index_dir = settings.data_dir / "books" / book_id / "index"
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


def _resolve_talk(book_id: str, speaker_id: str, reader_para: int):
    """Shared 404/403 checks for talk endpoints: returns (book, speaker or None, clamped reader_para)."""
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    if speaker_id != "narrator" and not any(c.id == speaker_id for c in book.characters):
        raise HTTPException(404, "Unknown speaker")
    reader_para = min(reader_para, len(book.paragraphs) - 1)
    speaker = next((c for c in book.characters if c.id == speaker_id), None)
    if speaker and book.kind == "novel" and speaker.first_para > reader_para:
        raise HTTPException(403, f"{speaker.name} has not appeared yet at this point in the story.")  # server-side gate
    return book, speaker, reader_para


@app.post("/api/retrieve", dependencies=[Depends(require_access)])
async def retrieve(req: RetrieveRequest, request: Request):
    if not retrieve_bucket.allow(client_ip(request)):
        raise HTTPException(429, "Too many lookups; wait a moment.")
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para)
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, req.k, **_series_context(book))
    out["instructions"] = build_instructions(book, req.speaker_id, out["story_so_far"], out["passages"])
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
    if not answer_bucket.allow(client_ip(request)):
        raise HTTPException(429, "Too many questions; wait a moment.")
    book, _, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para)
    t0 = time.monotonic()
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, req.k, **_series_context(book))
    t_retrieved = time.monotonic()
    retrieve_ms = int((t_retrieved - t0) * 1000)
    system, user = build_answer_prompt(book, req.speaker_id, out["story_so_far"], out["passages"], req.query)

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


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    voice: str


@app.post("/api/speak", dependencies=[Depends(require_access)])
async def speak(req: SpeakRequest, request: Request):
    if not voices.is_known_voice(req.voice):
        raise HTTPException(400, f"Unknown voice '{req.voice}'.")
    if not tts_cache.is_cached(req.text, req.voice) and not tts_bucket.allow(client_ip(request)):
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


def _set_status(book_id: str, status: str, message: str | None = None) -> None:
    _upload_status[book_id] = ingest_upload.write_status(book_id, status, message)


async def _ingest(book_id: str, title: str, author: str, kind: str, data: bytes, ext: str, token: int) -> None:
    """Background ingest: CPU work in a thread, then (vertex only) index the chunks, then publish book.json.

    book.json is written last, so a processing or failed upload is never listed or readable. Never raises.
    """
    _ingest_guard.mark_started(token)
    try:
        book, chunks = await asyncio.to_thread(ingest_upload.process_upload, book_id, title, author, kind, data, ext)
        if settings.retrieval_backend == "vertex":
            from .gcp.indexing import index_book
            embedder, store = _vertex_deps()
            await index_book(book, chunks, embedder, store)
        await asyncio.to_thread(ingest_upload.publish_book, book)
        _index_for.cache_clear()
        _set_status(book_id, "ready")
    except Exception as e:
        ingest_upload.discard_book(book_id)
        if isinstance(e, ingest_upload.UploadError):
            _set_status(book_id, "error", str(e))
        else:  # never leak details to the client
            logging.getLogger(__name__).warning("upload ingest failed: %s", type(e).__name__)
            _set_status(book_id, "error", "The file could not be processed.")
    finally:
        _ingest_guard.release(token)


@app.post("/api/books/upload", status_code=202, dependencies=[Depends(require_access)])
async def upload_book(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      title: str = Form(..., min_length=1, max_length=120),
                      author: str = Form("", max_length=120),
                      kind: Literal["novel", "textbook"] = Form(...)):
    if not upload_bucket.allow(client_ip(request)):
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
    st = _upload_status.get(book_id)
    if st is None and books._ID.match(book_id):
        st = ingest_upload.read_status(book_id)
        if st and st.get("status") == "processing":  # not in memory: the server restarted mid-ingest
            st = {"status": "error", "message": "Processing was interrupted; please upload again."}
    if st is None:
        raise HTTPException(404, "Book not found")
    return st


# ---------- avatars (stretch) ----------

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _avatar_dir(book_id: str) -> Path:
    return settings.data_dir / "cache" / "avatar" / book_id


@app.get("/api/avatars/{book_id}")
def list_avatars(book_id: str):
    """Chunk ids that have a pre-rendered avatar clip, so the reader only asks for clips that exist."""
    if not _SAFE_NAME.match(book_id):
        raise HTTPException(404, "Book not found")
    d = _avatar_dir(book_id)
    return sorted(p.stem for p in d.glob("*.mp4")) if d.is_dir() else []


_TTS_URL = re.compile(r"^/media/tts/([0-9a-f]{64})\.mp3$")
avatar_bucket = TokenBucket(capacity=12, per_seconds=60)  # every render spends Boson avatar credit
_avatar_locks: dict[str, asyncio.Lock] = {}


class LipSyncRequest(BaseModel):
    audio_url: str = Field(max_length=120)
    voice: str = Field(max_length=120)


@app.post("/api/avatar/talk", dependencies=[Depends(require_access)])
async def lipsync(req: LipSyncRequest, request: Request):
    """Lip-synced clip of one answer sentence: the voice's face speaking that cached TTS audio (~10 s to render).

    Only audio this server synthesised (/media/tts/<key>.mp3) and only voices with a face are accepted; clips are
    cached by audio key, so the same sentence never renders twice.
    """
    m = _TTS_URL.match(req.audio_url)
    face = voices.face_path(req.voice) if voices.is_known_voice(req.voice) else None
    if not m or face is None:
        raise HTTPException(400, "Lip-sync needs audio from /api/speak and a voice that has a face.")
    audio = settings.data_dir / "cache" / "tts" / f"{m.group(1)}.mp3"
    if not audio.is_file():
        raise HTTPException(404, "That audio is not cached.")
    key = f"{m.group(1)}-{hashlib.sha256(face.read_bytes()).hexdigest()[:16]}"
    clip = _lipsync_dir() / f"{key}.mp4"
    lock = _avatar_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if not clip.is_file():
            if not avatar_bucket.allow(client_ip(request)):
                raise HTTPException(429, "Too many lip-sync renders; wait a moment.")
            data = audio.read_bytes()
            try:
                vid = await boson.create_video(base64.b64encode(face.read_bytes()).decode(), base64.b64encode(data).decode(),
                                               image_mime=voices.FACE_EXTS[face.suffix.lstrip(".").lower()],
                                               seconds=len(data) / 16000)  # 128 kbit/s mp3
                await boson.wait_video(vid, interval=1, timeout=120)
                video = await boson.download_video(vid)
            except (BosonError, RuntimeError) as e:
                raise boson_http_error(e)
            clip.parent.mkdir(parents=True, exist_ok=True)
            tmp = clip.with_suffix(".tmp")
            tmp.write_bytes(video)
            tmp.replace(clip)
    _avatar_locks.pop(key, None)
    return {"video_url": f"/media/avatar/{clip.name}"}


@app.get("/api/avatar/{chunk_id}")
def avatar(chunk_id: str, book: str):
    if not (_SAFE_NAME.match(chunk_id) and _SAFE_NAME.match(book)):
        raise HTTPException(404, "No avatar clip")
    path = _avatar_dir(book) / f"{chunk_id}.mp4"
    if not path.is_file():
        raise HTTPException(404, "No avatar clip for this line")
    return FileResponse(path, media_type="video/mp4")


# ---------- voice cloning ----------

@app.post("/api/voices/clone", dependencies=[Depends(require_access)])
async def clone_voice(request: Request, audio: UploadFile = File(...), transcript: str = Form(..., max_length=2000),
                      name: str = Form(..., max_length=60), consent: str = Form("false"),
                      face: UploadFile | None = File(None)):
    if not clone_bucket.allow(client_ip(request)):
        raise HTTPException(429, "Too many voice uploads; wait a moment.")
    data = await audio.read(voices.MAX_BYTES + 1)  # bounded read: never buffer an unbounded upload
    image = await face.read(voices.MAX_FACE_BYTES + 1) if face is not None else None
    face_ext = Path(face.filename or "").suffix if face is not None else ""
    try:
        voices.validate_clone_request(data, transcript, name, consent)
        if image is not None and (len(image) > voices.MAX_FACE_BYTES or face_ext.lower().lstrip(".") not in voices.FACE_EXTS):
            raise voices.VoiceInputError("The face must be a PNG, JPEG or WebP image of at most 5 MB.")
    except voices.VoiceInputError as e:
        raise HTTPException(400, str(e))
    try:
        voice_id = await boson.create_voice(base64.b64encode(data).decode(), transcript, description=name)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
    voices.save_voice(name, voice_id)
    face_url = voices.save_face(voice_id, image, face_ext) if image is not None else None
    return {"voice": voice_id, "name": name, "face": face_url}


@app.get("/api/voices")
def list_voices():
    return voices.load_voices()


@app.get("/api/voices/faces")
def list_voice_faces():
    """voice id -> portrait URL for cloned voices that have a face (shown for any speaker using that voice)."""
    return voices.load_faces()


# ---------- single-service deploy: serve the built frontend from the same origin (no CORS, one container) ----------
_dist = settings.data_dir.parent / "frontend" / "dist"
if _dist.is_dir():  # registered last so every /api, /media and /portraits route wins
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
