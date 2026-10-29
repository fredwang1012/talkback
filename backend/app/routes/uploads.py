"""User uploads and their processing: upload, describe, status, enrich, kind change, delete, equation pictures.
Logs as "app.main" (these lines were logged from main.py before the routers split, so they read the same)."""
import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import books, deps, indexes, ingest_upload, processing, users
from ..config import settings
from ..enrich import describe as describe_book
from ..enrich import pipeline as enrich_pipeline


router = APIRouter()


_EQ_ID = re.compile(r"^(?:e\d{1,5}|m\d{1,6})$")  # display equation / inline formula


@router.get("/api/books/{book_id}/equations/{eq_id}.png")
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


@router.post("/api/books/upload", status_code=202, dependencies=[Depends(deps.require_access)])
async def upload_book(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                      title: str = Form(..., min_length=1, max_length=120),
                      author: str = Form("", max_length=120),
                      kind: Literal["auto", "novel", "textbook"] = Form("auto")):
    if not deps.upload_bucket.allow(deps.rate_key(request)):
        raise HTTPException(429, "Too many uploads; wait a moment.")
    token = processing._ingest_guard.acquire()  # no await between the check and the hold
    if token is None:
        raise HTTPException(429, f"{processing.UPLOAD_QUEUE_MAX} books are already being processed; try again shortly.")
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
        if hash_key in processing._hash_claims:   # the same file is being added right now
            processing._ingest_guard.release(token)
            return processing._duplicate("processing", processing._hash_claims[hash_key])
        processing._hash_claims[hash_key] = ""   # claimed before the lookup (no await since the check): a twin request 409s above
        try:
            found = await asyncio.to_thread(ingest_upload.find_upload, digest, ident)
            conflict = processing._upload_conflict(found)
            if conflict:
                processing._hash_claims.pop(hash_key, None)
                processing._ingest_guard.release(token)
                return conflict
            book_id, _ = ingest_upload.make_book_dir(title)
            processing._hash_claims[hash_key] = book_id
            users.claim_book(book_id)  # PER_USER: this upload belongs to the caller
            path = ingest_upload.upload_path(book_id, ext)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            await asyncio.to_thread(ingest_upload.record_upload_hash, digest, book_id, ident)
            processing._set_status(book_id, "processing", "Queued")
        except BaseException:
            processing._hash_claims.pop(hash_key, None)
            raise
        # releases the guard and the hash claim
        background.add_task(processing._ingest, book_id, title, author.strip(), kind, data, ext, token, hash_key)
    except BaseException:
        processing._ingest_guard.release(token)
        raise
    return {"book_id": book_id, "status": "processing"}


@router.post("/api/books/describe", dependencies=[Depends(deps.require_access)])
async def describe_upload(request: Request, file: UploadFile = File(...)):
    """Title and author for the Add book form, read from the chosen file (#83). Nothing is stored.

    May cost one Gemini Flash call, so: signed-in accounts only under PER_USER, a per-caller bucket here, and a global
    bucket, daily cap and budget reservation in describe_book. Only the start of a .txt is read (a PDF needs its whole
    structure, up to the upload cap); parsing runs under a small global limit (429 when full) with a timeout.
    """
    users.require_account("Sign in to add books.")
    if not deps.describe_bucket.allow(deps.rate_key(request)):
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


@router.get("/api/books/{book_id}/status")
def book_status(book_id: str):
    users.guard_book(book_id)
    if book_id in processing._delete_status:   # a delete's background part is running or failed
        return processing._delete_status[book_id]
    st = processing._upload_status.get(book_id)
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


@router.post("/api/books/{book_id}/enrich", status_code=202, dependencies=[Depends(deps.require_access)])
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
    if (book_id in processing._kind_claims or book_id in processing._enrich_jobs
            or book_id in processing._equation_jobs):   # a job or kind change is running
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    if state == "done":   # a stale "Try again" on a book that has finished meanwhile: just report it
        return st
    if state not in ("awaiting_confirmation", "error", "refused"):
        raise HTTPException(409, "This book is not waiting for confirmation.")
    if not settings.upload_enrich:   # UPLOAD_ENRICH=false: no paid processing, not even a retry
        raise HTTPException(409, processing.ENRICH_OFF)
    retries = int(st.get("enrich_retries") or 0)
    if retry and retries >= processing.MAX_ENRICH_RETRIES:
        raise HTTPException(409, processing.TOO_MANY_TRIES)
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    textbook = book.kind == "textbook"
    flag = {"summaries_only": True} if textbook else {}
    reason = enrich_pipeline.check_caps(enrich_pipeline.estimate_for(book), textbook=textbook, book_id=book_id)  # re-checked at start
    if reason:
        processing._set_enrich(book_id, status="refused", message=reason, **flag)
        return processing._upload_status[book_id]
    extra = {"enrich_retries": retries + 1} if retry else {}
    processing._set_enrich(book_id, status="running", stage="queued", progress=0.0, message="Waiting to start",
                           **flag, **extra)
    background.add_task(processing._run_enrichment, book_id, resume=retry)
    return processing._upload_status[book_id]


class KindBody(BaseModel):
    kind: Literal["novel", "textbook"]


@router.post("/api/books/{book_id}/kind", status_code=202, dependencies=[Depends(deps.require_access)])
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
    if (st.get("enrich") or {}).get("status") == "running" or book_id in processing._enrich_jobs \
            or book_id in processing._kind_claims or book_id in processing._equation_jobs:
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    if book.kind == body.kind:
        return st
    processing._kind_claims.add(book_id)
    try:
        def set_kind(b) -> None:
            b.kind = body.kind
        path = settings.data_dir / "books" / book_id / "book.json"
        rebuilt, pdf_bytes = await processing._reread_as_textbook(book) if body.kind == "textbook" else (None, None)
        if rebuilt is not None:
            try:  # applied to a fresh read under the lock: a write made during the re-read is kept
                book = await asyncio.to_thread(books.update_book, path,
                                               lambda b: ingest_upload.apply_textbook_text(b, rebuilt))
            except Exception as e:  # still move the shelf; the text they already have stays
                logging.getLogger("app.main").warning("textbook restore failed: %s", type(e).__name__)
                rebuilt = pdf_bytes = None
        if rebuilt is None:
            # a fresh read under the book's write lock (never the cached object, never a stale copy of it)
            book = await asyncio.to_thread(books.update_book, path, set_kind)
        indexes._index_for.cache_clear()
        processing._set_status(book_id, "ready", kind=body.kind)
        if rebuilt is not None:
            await processing._reindex_moved_book(book, background)
            if any(p.kind == "math" or p.inline_math for p in book.paragraphs):
                processing._start_equations(book_id, pdf_bytes)
        # a novel's own summaries are kept when it moves to Textbooks (recap then uses them as before)
        needs = not book.characters if body.kind == "novel" else not book.summaries
        if settings.upload_enrich and needs:
            if processing._decide_enrichment(book_id, book):  # sets status "running" (queued) before the claim is released
                background.add_task(processing._run_enrichment, book_id)
        return processing._upload_status[book_id]
    finally:
        processing._kind_claims.discard(book_id)


@router.delete("/api/books/{book_id}", status_code=202, dependencies=[Depends(deps.require_access)])
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
    if book_id in processing._deleting:   # a retry while the background part runs: join it, do not 409 or delete twice
        return accepted
    book_dir = settings.data_dir / "books" / book_id
    if not book_dir.is_dir():
        raise HTTPException(404, "Book not found")
    # A resume needs the persisted tombstone (or this process's record of a failed delete). A folder without book.json
    # and without a tombstone is an upload still being ingested: never delete that from under its job.
    resuming = (book_dir / "deleting.json").is_file() or book_id in processing._delete_status
    # ...unless the server restarted mid-ingest: status.json still says "processing" but no job here owns the book
    # (the library shows it as interrupted), so nothing will ever finish it and it must stay deletable.
    interrupted = (book_id not in processing._upload_status and not processing._book_busy(book_id)
                   and (ingest_upload.read_status(book_id) or {}).get("status") == "processing")
    if processing._book_busy(book_id) or not (resuming or interrupted or (book_dir / "book.json").is_file()):
        raise HTTPException(409, "This book is still being processed; try again when it finishes.")
    # Claim synchronously (no await since the checks): blocks a concurrent kind change / enrichment / second delete.
    processing._kind_claims.add(book_id)
    processing._deleting[book_id] = time.monotonic()
    try:
        ids = await asyncio.to_thread(processing._tombstone_book, book_id)
    except BaseException:
        processing._kind_claims.discard(book_id)
        processing._deleting.pop(book_id, None)
        raise
    processing._deleted.add(book_id)   # from now on no background job writes this book back
    processing._forget_book(book_id)
    processing._delete_status[book_id] = {"status": "deleting", "message": "Removing the book"}
    background.add_task(processing._finish_delete, book_id, ids)
    return accepted
