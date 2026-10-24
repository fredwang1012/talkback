import asyncio
import base64
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import books, retrieval, tts_cache, voices
from .boson import BosonClient, BosonError
from .config import settings
from .personas import build_answer_prompt, build_instructions
from .ratelimit import TokenBucket
from .schema import Book

def _warm_up() -> None:
    """Load the embedding model and every book's index before the first question (the model takes ~17 s to load)."""
    try:
        for info in books.list_books():
            _index_for(info["id"])
    except Exception:  # never block startup; retrieval falls back to BM25 / loads lazily
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(asyncio.to_thread(_warm_up))
    yield
    task.cancel()


app = FastAPI(title="Talkback", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

(settings.data_dir / "cache").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.data_dir / "cache"), name="media")
app.mount("/portraits", StaticFiles(directory=settings.data_dir / "portraits", check_dir=False), name="portraits")

boson = BosonClient()
token_bucket = TokenBucket(capacity=10, per_seconds=60)
tts_bucket = TokenBucket(capacity=120, per_seconds=60)   # only cache misses (real Boson calls) consume this
clone_bucket = TokenBucket(capacity=5, per_seconds=60)


def client_ip(request: Request) -> str:
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
    return {"ok": True, "boson_key_set": bool(settings.boson_api_key), "access_required": bool(settings.access_token)}


@app.get("/api/books")
def get_books():
    return books.list_books()


@app.get("/api/books/{book_id}", response_model=Book)
def get_book(book_id: str):
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    return book


# ---------- realtime key ----------

@app.post("/api/realtime/token", dependencies=[Depends(require_access)])
async def realtime_token(request: Request):
    """Mint a fresh ephemeral Realtime key per call (never cached). The real API key never leaves the server."""
    if not token_bucket.allow(client_ip(request)):
        raise HTTPException(429, "Too many connection attempts; wait a moment.")
    try:
        return await boson.mint_client_secret(600)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)


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
        return VertexIndex(book, _LazyEmbedder(), _LazyStore())
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


@app.post("/api/retrieve")
async def retrieve(req: RetrieveRequest):
    book, speaker, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para)
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, req.k)
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


@app.post("/api/answer")
async def answer(req: RetrieveRequest):
    book, _, reader_para = _resolve_talk(req.book_id, req.speaker_id, req.reader_para)
    t0 = time.monotonic()
    out = await retrieval.aretrieve(book, _index_for(req.book_id), req.query, reader_para, req.k)
    retrieve_ms = int((time.monotonic() - t0) * 1000)
    system, user = build_answer_prompt(book, req.speaker_id, out["story_so_far"], out["passages"], req.query)

    async def stream():
        first_ms = None
        try:
            async for sentence in get_answerer().stream_sentences(system, user):
                if first_ms is None:
                    first_ms = int((time.monotonic() - t0) * 1000) - retrieve_ms
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
                      name: str = Form(..., max_length=60), consent: str = Form("false")):
    if not clone_bucket.allow(client_ip(request)):
        raise HTTPException(429, "Too many voice uploads; wait a moment.")
    data = await audio.read(voices.MAX_BYTES + 1)  # bounded read: never buffer an unbounded upload
    try:
        voices.validate_clone_request(data, transcript, name, consent)
    except voices.VoiceInputError as e:
        raise HTTPException(400, str(e))
    try:
        voice_id = await boson.create_voice(base64.b64encode(data).decode(), transcript, description=name)
    except (BosonError, RuntimeError) as e:
        raise boson_http_error(e)
    voices.save_voice(name, voice_id)
    return {"voice": voice_id, "name": name}


@app.get("/api/voices")
def list_voices():
    return voices.load_voices()


# ---------- single-service deploy: serve the built frontend from the same origin (no CORS, one container) ----------
_dist = settings.data_dir.parent / "frontend" / "dist"
if _dist.is_dir():  # registered last so every /api, /media and /portraits route wins
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
