"""Client of the public processing API (PUBLIC_API_URL): upload enrichment and Gemini-written answers for a clone
that has only a Boson key.

Used only when PUBLIC_API_URL is set and no local LLM is configured (`active()`). Spoiler filtering always stays
local: answers send only passages the local retrieval already filtered. If the service is closed, unreachable or
capped, enrichment falls back to the simple local cast (with a message saying why) and typed answers report why.
"""
import asyncio
import functools
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx
from pydantic import ValidationError

from . import books
from .config import settings
from .enrich import cast as cast_mod
from .enrich import pipeline
from .enrich.segment import SegItem, narrator_only, speaker_ids, validate_segments
from .schema import Book, Character, Narrator, Summary

log = logging.getLogger("talkback.public")

UNAVAILABLE_NOTE = "Characters detected with a simple fallback (the processing service is unavailable right now)."
LIMIT_NOTE = "Characters detected with a simple fallback (the free processing limit has been reached)."
ANSWER_UNAVAILABLE = "The Gemini answer service is unavailable right now; ask by voice or try again later."
ANSWER_LIMIT = "The free Gemini answers for today are used up; ask by voice or try again tomorrow."

POLL_INTERVAL = 2.0
BUSY_RETRY_SECONDS = 10.0
BUSY_WAIT_SECONDS = 180.0
JOB_WAIT_SECONDS = 26 * 60
MAX_POLL_ERRORS = 5
PROBE_TTL = 60.0


class PublicFailure(Exception):
    """kind: "unavailable" (closed, unreachable, failed) or "limit" (a spend cap or rate limit)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind


# ---------- when to use the service ----------

@functools.lru_cache(maxsize=1)
def gcp_credentials_usable() -> bool:
    """A GCP project is configured and Application Default Credentials load (checked once)."""
    if not settings.gcp_project:
        return False
    try:
        import google.auth
        google.auth.default()
        return True
    except Exception:
        return False


def local_llm_configured() -> bool:
    return bool(settings.anthropic_api_key) or gcp_credentials_usable()


def active() -> bool:
    """PUBLIC_API_URL is set and this machine has no LLM of its own."""
    return bool(settings.public_api_url.strip()) and not settings.public_api and not local_llm_configured()


def _base() -> str:
    return settings.public_api_url.strip().rstrip("/")


_probe: tuple[float, bool] | None = None


def mark_unavailable() -> None:
    global _probe
    _probe = (time.monotonic(), False)


def answers_available() -> bool:
    """The service answers its health check (cached for a minute). A closed service (403) or a dead host is False."""
    global _probe
    now = time.monotonic()
    if _probe is not None and now - _probe[0] < PROBE_TTL:
        return _probe[1]
    try:
        r = httpx.get(f"{_base()}/api/health", timeout=3.0)
        ok = r.status_code == 200 and bool(r.json().get("public"))
    except Exception:
        ok = False
    _probe = (now, ok)
    return ok


def _code(r: httpx.Response) -> str | None:
    try:
        return r.json().get("code")
    except Exception:
        return None


def _failure(r: httpx.Response) -> PublicFailure:
    """A refused request: 402 (budget) and 429 (rate limit) are limits; everything else is unavailability."""
    code = _code(r)
    if r.status_code in (402, 429):
        return PublicFailure("limit", code or str(r.status_code))
    return PublicFailure("unavailable", str(r.status_code))


# ---------- enrichment ----------

def _payload(book: Book) -> dict:
    return {"title": book.title, "author": book.author, "kind": "novel",
            "sections": [{"id": s.id, "title": s.title, "start_para": s.start_para} for s in book.sections],
            "paragraphs": [{"idx": p.idx, "section_id": p.section_id, "text": p.text} for p in book.paragraphs]}


async def _remote_enrich(book: Book, report: Callable[..., None], client: httpx.AsyncClient,
                         sleep: Callable[[float], Awaitable] = asyncio.sleep) -> dict:
    """Start a job and poll it to the end; returns the result dict. Raises PublicFailure."""
    payload = _payload(book)
    waited = 0.0
    while True:
        try:
            r = await client.post("/api/public/enrich", json=payload, timeout=httpx.Timeout(60.0, connect=10.0))
        except httpx.HTTPError as e:
            raise PublicFailure("unavailable", type(e).__name__) from e
        if r.status_code == 202:
            job = r.json()["job_id"]
            break
        if r.status_code == 429 and _code(r) == "busy" and waited < BUSY_WAIT_SECONDS:
            report(status="running", stage="queued", progress=0.0, message="Waiting for a free processing slot")
            await sleep(BUSY_RETRY_SECONDS)
            waited += BUSY_RETRY_SECONDS
            continue
        raise _failure(r)

    errors, waited = 0, 0.0
    while waited < JOB_WAIT_SECONDS:
        await sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        try:
            r = await client.get(f"/api/public/enrich/{job}", timeout=20.0)
        except httpx.HTTPError as e:
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                raise PublicFailure("unavailable", type(e).__name__) from e
            continue
        if r.status_code != 200:
            if r.status_code in (429, 500, 502, 503, 504) and errors + 1 < MAX_POLL_ERRORS:
                errors += 1
                continue
            raise _failure(r)
        errors = 0
        view = r.json()
        status = view.get("status")
        if status == "done" and isinstance(view.get("result"), dict):
            return view["result"]
        if status == "error":
            raise PublicFailure("limit" if view.get("code") in ("cap", "book_cap") else "unavailable", str(view.get("code")))
        report(status="running", stage=view.get("stage") or "queued", progress=min(float(view.get("progress") or 0.0), 0.99),
               message=view.get("message") or "Processing")
    raise PublicFailure("unavailable", "timeout")


def apply_result(book: Book, result: dict) -> None:
    """Merge the service's answer into `book`, validating it: the server is not trusted to keep the text intact."""
    n = len(book.paragraphs)
    chars: list[Character] = []
    seen: set[str] = set()
    for raw in (result.get("characters") or [])[:cast_mod.MAIN_MAX]:
        c = Character.model_validate(raw)
        if c.id in seen or not c.id:
            continue
        seen.add(c.id)
        c.first_para = max(0, min(c.first_para, n - 1))
        c.persona = c.persona.strip()[:600] or f"{c.name} speaks in their own way."
        c.portrait, c.main = None, True
        if c.voice not in cast_mod.VOICES:
            c.voice = "nora"
        if c.voice_tags not in cast_mod.VOICE_TAGS:
            c.voice_tags = ""
        chars.append(c)
    if not chars:
        raise ValueError("no characters in the result")
    book.characters = chars
    nar = result.get("narrator") or {}
    is_char = nar.get("is_character") if nar.get("is_character") in seen else None
    voice = next((c.voice for c in chars if c.id == is_char), nar.get("voice"))
    book.narrator = Narrator(voice=voice if voice in cast_mod.VOICES else "nora", is_character=is_char)

    speakers = speaker_ids(book)
    segs = result.get("segments") or {}
    for p in book.paragraphs:
        out = None
        raw = segs.get(str(p.idx))
        if isinstance(raw, list):
            try:
                items = [SegItem(speaker=str(s["speaker"]), text=str(s["text"]), emotion=s.get("emotion"),
                                 style=s.get("style")) for s in raw]
                out = validate_segments(p.text, items, speakers)
            except (ValidationError, KeyError, TypeError, AttributeError):
                out = None
        p.segments = out or narrator_only(p.text)
    book.summaries = sorted(
        (Summary(upto_para=max(0, min(int(s["upto_para"]), n - 1)), text=str(s["text"])[:4000])
         for s in (result.get("summaries") or []) if isinstance(s, dict) and s.get("text")),
        key=lambda s: s.upto_para)
    pipeline.rebuild_chunks(book)


async def _save(path: Path, book: Book) -> None:
    await asyncio.to_thread(books.save_book, path, book)


async def _local_fallback(book: Book, path: Path, note: str) -> None:
    def run() -> None:
        book.characters, book.narrator, book.summaries = [], None, []
        cast_mod.fallback_cast(book)
        for p in book.paragraphs:
            p.segments = narrator_only(p.text)
        pipeline.rebuild_chunks(book)
        books.save_book(path, book)
    await asyncio.to_thread(run)


async def enrich_book(book_id: str, report: Callable[..., None], *, indexer=None, client: httpx.AsyncClient | None = None,
                      sleep: Callable[[float], Awaitable] = asyncio.sleep) -> bool:
    """Enrich data/books/<id> through the public API; on failure use the simple local cast. Never raises."""
    path = settings.data_dir / "books" / book_id / "book.json"
    own = client is None
    try:
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        note = None
        client = client or httpx.AsyncClient(base_url=_base())
        try:
            report(status="running", stage="cast", progress=0.0, message=pipeline.MESSAGES["cast"])
            result = await _remote_enrich(book, report, client, sleep)
            apply_result(book, result)
        except PublicFailure as e:
            log.warning("public enrichment of %s failed: %s %s", book_id, e.kind, e)
            note = LIMIT_NOTE if e.kind == "limit" else UNAVAILABLE_NOTE
        except (ValidationError, ValueError, TypeError, KeyError) as e:  # a malformed answer is as good as none
            log.warning("public enrichment of %s returned a bad result: %s", book_id, type(e).__name__)
            note = UNAVAILABLE_NOTE
            book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        finally:
            if own:
                await client.aclose()
        if note:
            await _local_fallback(book, path, note)
        else:
            await _save(path, book)
        report(status="running", stage="index", progress=0.97, message=pipeline.MESSAGES["index"])
        if indexer is not None:
            await indexer(book)
        report(status="done", stage="done", progress=1.0, degraded=True if note else None,
               message=note or "Characters, recap and search are ready.")
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("public enrichment of %s failed: %s", book_id, type(e).__name__)
        report(status="error", stage="cast", message=pipeline.FAILED)
        return False


# ---------- answers ----------

def answer_payload(book: Book, speaker, speaker_id: str, out: dict, query: str, spoilers: bool) -> dict:
    """The request for /api/public/answer from locally retrieved (already spoiler-filtered) passages."""
    return {"title": book.title, "kind": book.kind, "speaker_id": speaker_id,
            "speaker_name": speaker.name if speaker else "", "persona": speaker.persona if speaker else "",
            "story_so_far": out["story_so_far"], "query": query, "spoilers": spoilers,
            "passages": [{k: v for k, v in p.items() if k in ("book", "para_start", "para_end", "text")}
                         for p in out["passages"]]}


async def stream_answer(payload: dict, client: httpx.AsyncClient | None = None) -> AsyncIterator[dict]:
    """Yield the service's `sentence` messages. Raises PublicFailure (before any sentence for a refused request)."""
    own = client is None
    client = client or httpx.AsyncClient(base_url=_base(), timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        try:
            async with client.stream("POST", "/api/public/answer", json=payload) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise _failure(r)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "sentence" and isinstance(msg.get("text"), str):
                        yield msg
                    elif msg.get("type") == "error":
                        raise PublicFailure("unavailable", "error line")
        except httpx.HTTPError as e:
            raise PublicFailure("unavailable", type(e).__name__) from e
    finally:
        if own:
            await client.aclose()
