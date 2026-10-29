import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from . import books, deps, gemini, indexes, processing, users
from .config import settings
from .routes import avatar, cast, faces, health, library, live, realtime, talk, tts, uploads, voices
from .uploadlimit import UploadSizeLimit


def _warm_up() -> None:
    """Load the embedding model and every book's index before the first question (the model takes ~17 s to load)."""
    if settings.auto_rechunk:
        books.schedule_all_upgrades()  # out-of-date tts_chunks are rebuilt on a background thread (no API calls)
    try:
        for info in books.list_books():
            indexes._index_for(info["id"])
        if settings.retrieval_backend == "vertex":  # first Vertex call pays auth + TLS setup (~5 s): pay it now
            indexes._vertex_deps()[0].embed_query("warm up")
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
    await gemini.resolve()   # GCP_PROJECT + GEMINI_API_KEY: the ADC probe runs here, off the loop (app/gemini.py)
    task = asyncio.create_task(asyncio.to_thread(_warm_up))
    resume = asyncio.create_task(processing._resume_enrichments())   # no job survives a restart: pick up the interrupted ones
    reindex = asyncio.create_task(processing._retry_failed_indexes())   # Vertex indexes a transient error left in state "error"
    backfill = asyncio.create_task(processing._backfill_local_embeddings())   # local: e5 vectors for books that have none yet
    orphans = None
    if settings.retrieval_backend == "vertex":   # datapoints a delete could not remove: retried, paced, in the background
        from .gcp import orphans as vertex_orphans
        orphans = asyncio.create_task(vertex_orphans.retry_forever(lambda: indexes._vertex_deps()[1]))
    yield
    if orphans is not None:
        orphans.cancel()
    task.cancel()
    resume.cancel()
    reindex.cancel()
    backfill.cancel()


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


class LipsyncFiles(StaticFiles):
    """/media/avatar. With PER_USER a caller is served only their own lip-sync clips (a face is a real person's)."""

    async def get_response(self, path: str, scope):
        if settings.per_user:
            ident = await users.identify_async(Headers(scope=scope))
            return await StaticFiles(directory=users.lipsync_dir(ident), check_dir=False).get_response(path, scope)
        return await super().get_response(path, scope)


deps._lipsync_dir().mkdir(parents=True, exist_ok=True)
app.mount("/media/avatar", LipsyncFiles(directory=deps._lipsync_dir()), name="lipsync")


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

# The routes, in the order they were registered when they all lived here (FastAPI matches in order:
# /api/voices/account before /api/voices/{voice_id}); the frontend mount below stays last.
for _routes in (health, library, realtime, tts, talk, uploads, avatar, voices, live, faces, cast):
    app.include_router(_routes.router)


# ---------- single-service deploy: serve the built frontend from the same origin (no CORS, one container) ----------
_dist = settings.data_dir.parent / "frontend" / "dist"
if _dist.is_dir():  # registered last so every /api, /media and /portraits route wins
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


if settings.public_api:  # PUBLIC_API=true through any entry point: expose only the public processing API
    from .public_api import app  # noqa: E402,F811
