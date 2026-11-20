"""Upload processing behind the routes: ingest, Vertex / local indexing, character enrichment, equations,
startup resumes and the delete machinery, with their in-memory state.
Logs as "app.main" (these lines were logged from main.py before the routers split, so they read the same)."""
import asyncio
import json
import logging
import time
from functools import lru_cache
from pathlib import Path

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse

from . import books, deps, indexes, ingest_upload, public_client, recap, retrieval, users
from .config import settings
from .enrich import equations as enrich_equations
from .enrich import pipeline as enrich_pipeline
from .enrich.detect import detect_kind
from .schema import Book


_upload_status: dict[str, dict] = {}


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
            log = logging.getLogger("app.main")
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
INDEX_RETRY_WAITS_S = (30.0, 90.0)   # a failed indexing run (often Vertex 429s while big deletes run) is retried after these


async def _index_with_retries(book: Book, chunks, embedder, store, on_progress=None) -> bool:
    """index_book, retried after each INDEX_RETRY_WAITS_S wait (Vertex 429s while big deletes or other books run).
    Re-running is safe: batchCreate replaces objects that already exist (409 -> delete + create). False when the book
    was deleted while waiting; the last failure is raised."""
    from .gcp.indexing import index_book
    for attempt, wait in enumerate((*INDEX_RETRY_WAITS_S, None)):
        try:
            await index_book(book, chunks, embedder, store, on_progress=on_progress)
            return True
        except _BookDeleted:
            raise
        except Exception as e:
            if wait is None or book.id in _deleted:
                raise
            logging.getLogger("app.main").warning("vertex indexing attempt %d failed (%s %s); retrying in %.0f s",
                                                attempt + 1, type(e).__name__, getattr(e, "status", ""), wait)
            await asyncio.sleep(wait)
            if book.id in _deleted:
                return False
    return True


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
            embedder, store = indexes._vertex_deps()
            if not await _index_with_retries(book, chunks, embedder, store,
                                             on_progress=lambda done, tot: _index_progress(book_id, done, tot)):
                return   # deleted while waiting to retry
        final = {"state": "done", "done": total, "total": total}
    except _BookDeleted:
        pass
    except Exception as e:  # the book stays readable and keeps the BM25 fallback
        if book_id not in _deleted:
            logging.getLogger("app.main").warning("vertex indexing failed: %s %s", type(e).__name__, getattr(e, "status", ""))
            final = {"state": "error", "done": 0, "total": total, "message": INDEX_FAILED_MESSAGE}
    finally:
        _index_jobs.discard(book_id)
        indexes._index_for.cache_clear()
        pending = _index_writes.pop(book_id, None)
        if pending is not None:   # a throttled progress write must not land after the final one
            await asyncio.gather(pending, return_exceptions=True)
        _index_written_at.pop(book_id, None)
    if final is not None and book_id not in _deleted:
        _upload_status[book_id] = _index_status(book_id, final)
        await asyncio.to_thread(_write_current_status, book_id)


# Local backend: meaning-based search. Each published upload gets e5 passage embeddings in the background, and at
# startup so do the built-in books and older uploads that have none yet (no embeddings ship in the repo), one book at a
# time (bounded CPU and RAM on a laptop); BM25 serves until they are saved. A book re-chunked meanwhile (kind change)
# is re-embedded once the running job ends; a deleted book stops its job between batches.
_local_embed_slot = _loop_semaphore(1)
_local_embed_jobs: set[str] = set()
_local_embed_again: dict[str, list] = {}   # book id -> chunks requested while its job was running
_local_embed_tasks: set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def _local_embed_available() -> bool:
    """sentence-transformers is installed (the optional "embed" extra); checked without importing it."""
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def _local_embed_on() -> bool:
    return settings.retrieval_backend == "local" and settings.local_embed_uploads


def _start_local_embed(book: Book, chunks) -> None:
    """Fire-and-forget: the upload's enrichment does not wait for its embeddings."""
    task = asyncio.create_task(_embed_locally(book, chunks))
    _local_embed_tasks.add(task)
    task.add_done_callback(_local_embed_tasks.discard)


async def _embed_locally(book: Book, chunks) -> None:
    await _embed_book_locally(book.id, chunks)


async def _embed_book_locally(book_id: str, chunks) -> None:
    """Embed a published book's chunks with the local e5 model and save them next to chunks.json. Never raises.
    Uploads report progress in status.json["index"] (the upload card's "Indexing" line); built-in books only log (a
    status.json there would dirty a clone's tree). Skipped (BM25 only) when sentence-transformers is missing; a model
    that fails to load is recorded as index state "error" (uploads)."""
    log = logging.getLogger("app.main")
    total = len(chunks)
    uploaded = book_id.startswith("u-")
    set_index = _set_index if uploaded else (lambda *_a, **_k: None)

    def progress(done: int, tot: int) -> None:
        if uploaded:
            _index_progress(book_id, done, tot)
        elif book_id in _deleted:
            raise _BookDeleted(book_id)

    if not _local_embed_on() or not chunks or book_id in _deleted:
        return
    if book_id in _local_embed_jobs:   # running: embed the newer chunks once it ends
        _local_embed_again[book_id] = chunks
        return
    if not _local_embed_available():
        log.info("local search for %s stays keyword-only: sentence-transformers is not installed", book_id)
        return
    _local_embed_jobs.add(book_id)
    final: dict | None = None
    try:
        set_index(book_id, state="queued", done=0, total=total)
        async with _local_embed_slot():
            if book_id in _deleted:
                return
            embedder = await asyncio.to_thread(indexes._embedder)   # first use loads the model (seconds): off the loop
            if embedder is None or not hasattr(embedder, "embed_passages"):
                log.warning("local embeddings for %s skipped: the e5 model could not be loaded; keyword search only",
                            book_id)
                final = {"state": "error", "done": 0, "total": total, "message": INDEX_FAILED_MESSAGE}
            else:
                set_index(book_id, state="running", done=0, total=total)
                log.info("embedding %s locally: %d chunks", book_id, total)
                from . import local_index
                emb = await local_index.embed_chunks(chunks, embedder, on_progress=progress)
                if book_id in _deleted:
                    raise _BookDeleted(book_id)
                index_dir = settings.data_dir / "books" / book_id / "index"
                if await asyncio.to_thread(local_index.save_embeddings, index_dir, chunks, emb):
                    log.info("embedded %s locally: meaning-based search on", book_id)
                final = {"state": "done", "done": total, "total": total}
    except _BookDeleted:
        _local_embed_again.pop(book_id, None)
    except Exception as e:   # the book keeps BM25
        if book_id not in _deleted:
            log.warning("local embedding failed for %s: %s", book_id, type(e).__name__)
            final = {"state": "error", "done": 0, "total": total, "message": INDEX_FAILED_MESSAGE}
    finally:
        _local_embed_jobs.discard(book_id)
        indexes._index_for.cache_clear()
        pending = _index_writes.pop(book_id, None)
        if pending is not None:   # a throttled progress write must not land after the final one
            await asyncio.gather(pending, return_exceptions=True)
        _index_written_at.pop(book_id, None)
    if final is not None and uploaded and book_id not in _deleted:
        _upload_status[book_id] = _index_status(book_id, final)
        await asyncio.to_thread(_write_current_status, book_id)
    again = _local_embed_again.pop(book_id, None)
    if again is not None and book_id not in _deleted:
        await _embed_book_locally(book_id, again)


def _books_missing_embeddings() -> list[tuple[str, list]]:
    """Worker thread: (book id, chunks from chunks.json) of every published book whose local index has no matching
    embeddings.npy; uploads first, then built-in books, each group by id."""
    from .local_index import has_embeddings, load_chunks
    root = settings.data_dir / "books"
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        index_dir = d / "index"
        if (not books._ID.match(d.name) or not (d / "book.json").is_file() or (d / "deleting.json").exists()
                or not (index_dir / "chunks.json").is_file()):
            continue
        try:
            chunks = load_chunks(index_dir)
            if chunks and not has_embeddings(index_dir, len(chunks)):
                out.append((d.name, chunks))
        except Exception as e:   # one unreadable index never stops the others
            logging.getLogger("app.main").warning("local index of %s unreadable: %s", d.name, type(e).__name__)
    return sorted(out, key=lambda x: (not x[0].startswith("u-"), x[0]))


async def _backfill_local_embeddings() -> None:
    """Startup (local backend only): embed the uploads made before local embeddings existed, then the built-in books,
    one at a time in the background. Awaited one by one, so an upload published meanwhile takes the next slot ahead of
    the remaining built-in books. Never raises."""
    if not _local_embed_on() or not _local_embed_available():
        return
    try:
        todo = await asyncio.to_thread(_books_missing_embeddings)
        if todo:
            logging.getLogger("app.main").info("local embeddings to build at startup: %d book(s)", len(todo))
        for book_id, chunks in todo:
            if book_id not in _deleted:
                await _embed_book_locally(book_id, chunks)
    except Exception as e:
        logging.getLogger("app.main").warning("local embedding backfill failed: %s", type(e).__name__)


def _set_enrich(book_id: str, **fields) -> None:
    """Record the novel enrichment sub-status next to the (ready) ingest status."""
    if book_id in _deleted:
        return
    retries = fields.pop("enrich_retries", None)
    enrich = {k: v for k, v in fields.items() if v is not None}
    extra = {"enrich_retries": retries} if retries is not None else {}
    _upload_status[book_id] = ingest_upload.write_status(book_id, "ready", enrich=enrich, index=_mem_index(book_id), **extra)


# ENRICH_PARALLEL uploads enriched at once (default 1000: no practical limit). Rate limits are handled by the shared
# Gemini pacer (app/pacer.py) and UPLOAD_CONCURRENCY per book, not by this semaphore.
_enrich_lock = asyncio.Semaphore(max(1, settings.enrich_parallel))


INDEX_FAILED_MESSAGE = "Search indexing failed; using basic search."


async def _enrich_indexer(book: Book) -> None:
    """Final stage: index the book in Vertex if the upload's own indexing did not finish (a complete index is kept as it
    is; see below). The local index needs no change. A failure (after the
    store's and embedder's own retries) is recorded as index state "error", so retrieval falls back to BM25 and the
    next start retries it (_retry_failed_indexes); it is then raised for the pipeline, which finishes the book anyway."""
    if settings.retrieval_backend == "vertex":
        from .gcp.indexing import load_chunks
        done = (_mem_index(book.id) or (ingest_upload.read_status(book.id) or {}).get("index") or {}).get("state") == "done"
        if done:
            # The upload already indexed every chunk. A re-index would only add the "speaker" field, which no search
            # reads, at the price of re-embedding the book and rewriting every object (409 -> delete -> create), which
            # under Vertex write limits kept failing with 429 and dropped finished books to basic search.
            return
        embedder, store = indexes._vertex_deps()
        chunks = await asyncio.to_thread(load_chunks, book)
        while book.id in _index_jobs:   # the upload's own indexing job is still queued or running: never overlap it
            await asyncio.sleep(1.0)
        _index_jobs.add(book.id)
        try:
            async with _index_slot():
                await _index_with_retries(book, chunks, embedder, store)
            if book.id not in _deleted:
                _set_index(book.id, state="done", done=len(chunks), total=len(chunks))
        except Exception as e:
            if book.id not in _deleted:
                logging.getLogger("app.main").warning("vertex re-indexing failed: %s %s", type(e).__name__,
                                                    getattr(e, "status", ""))
                _set_index(book.id, state="error", done=0, total=len(chunks), message=INDEX_FAILED_MESSAGE)
            raise
        finally:
            _index_jobs.discard(book.id)
            indexes._index_for.cache_clear()


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
        logging.getLogger("app.main").warning("retrying failed indexes failed: %s", type(e).__name__)


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
            logging.getLogger("app.main").warning("settling %s failed: %s", book_id, type(e).__name__)


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
        indexes._index_for.cache_clear()


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
    """Startup: restart the enrichment of uploads that a restart interrupted. Same caps and the same enrichment
    semaphore (ENRICH_PARALLEL) as a fresh job; stages already saved are not paid for again. With PER_USER on, the owner comes
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
            jobs.append(asyncio.create_task(_resume_one(book_id, ns)))   # up to ENRICH_PARALLEL run at once (_enrich_lock)
        _resume_tasks.update(jobs)
        for t in jobs:
            t.add_done_callback(_resume_tasks.discard)
        await asyncio.gather(*jobs, return_exceptions=True)
    except Exception as e:
        logging.getLogger("app.main").warning("resuming enrichment failed: %s", type(e).__name__)


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
    """Background ingest: CPU work in a thread, then publish book.json (readable, local BM25 retrieval), then index the
    chunks in the background with progress in status.json["index"]: Vertex (awaited before enrichment), or local e5
    embeddings (RETRIEVAL_BACKEND=local, not awaited; _embed_locally).

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
            logging.getLogger("app.main").warning("upload ingest failed: %s", type(e).__name__)
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
    elif ready is not None and _local_embed_on() and book_id not in _deleted:
        _start_local_embed(ready, chunks)   # meaning-based search once embedded; BM25 meanwhile
    if ready is not None and settings.upload_enrich and book_id not in _deleted:
        current = books.load_book(book_id)   # the reader may have moved it to the other shelf (or deleted it) meanwhile
        if current is None or current.kind != kind:
            return
        try:
            await _plan_enrichment(book_id, current)
        except Exception as e:  # the book stays readable with the narrator
            logging.getLogger("app.main").warning("enrichment planning failed: %s", type(e).__name__)
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
    indexes._index_for.cache_clear()
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
        logging.getLogger("app.main").warning("equations of %s failed: %s", book_id, type(e).__name__)
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
        logging.getLogger("app.main").info("equations of %s: %s", book_id, note)


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
        logging.getLogger("app.main").warning("textbook restore failed: %s", type(e).__name__)
        return None, None


async def _reindex_moved_book(book: Book, background: BackgroundTasks) -> None:
    """Retrieval index for a book whose text was re-read, as an upload builds it: the local chunks always, then (vertex)
    the old text's leftover datapoints removed and the new chunks embedded in the background, or (local) the new
    chunks embedded with e5 in the background."""
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
    indexes._index_for.cache_clear()
    if settings.retrieval_backend == "vertex":
        background.add_task(_reindex_vertex, book, chunks, stale)
    elif _local_embed_on():
        background.add_task(_embed_locally, book, chunks)


async def _reindex_vertex(book: Book, chunks, stale: list[str]) -> None:
    """Drop the old text's datapoints that the new chunks do not overwrite, then index the new chunks. Never raises."""
    if stale:   # a leftover datapoint is still bound by its own para_end (spoiler rule holds); retried later
        await _vertex_delete_or_remember(stale)
    await _index_in_background(book, chunks)


async def _vertex_delete_or_remember(ids: list[str]) -> None:
    """Delete datapoints; whatever Vertex refuses goes to the background cleanup list (app/gcp/orphans.py). Never
    raises. Orphans are harmless: every search filters on book_id (query filter and VertexIndex post-filter)."""
    from .gcp import orphans
    try:
        refused = await indexes._vertex_deps()[1].delete(ids) or []
    except Exception as e:
        logging.getLogger("app.main").warning("vertex delete failed (%s): %d id(s) kept for cleanup",
                                            type(e).__name__, len(ids))
        refused = list(ids)
    await asyncio.to_thread(orphans.add, list(refused))


def _book_busy(book_id: str) -> bool:
    """True while an ingest, Vertex indexing (running or queued), enrichment or kind change for this book is running.
    Local embedding is not: it stops by itself on a delete and re-runs after a kind change."""
    st = _upload_status.get(book_id) or {}
    index_busy = (st.get("index") or {}).get("state") in ("running", "queued") and book_id not in _local_embed_jobs
    return (st.get("status") == "processing" or (st.get("enrich") or {}).get("status") == "running"
            or index_busy or book_id in _index_jobs
            or book_id in _enrich_jobs or book_id in _kind_claims)


def _forget_book(book_id: str) -> None:
    """Drop every in-memory trace of a book (library listing, serialised bodies, indexes, status)."""
    _upload_status.pop(book_id, None)
    deps._book_bodies.pop(book_id, None)
    for k in [k for k in books._cache if Path(k).parent.name == book_id]:
        books._cache.pop(k, None)
    books._series_index = None
    indexes._index_for.cache_clear()


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
    """Background: the Vertex datapoints first, then every file. Vertex never blocks the delete: the ids it refuses go
    to the background cleanup list and the files go anyway. A file that cannot be removed keeps the folder and
    deleting.json, so a retry resumes. Never raises; releases the claim taken by delete_book."""
    log = logging.getLogger("app.main")
    try:
        if ids:
            await _vertex_delete_or_remember(ids)
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
