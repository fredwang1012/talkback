"""Client of the public processing API (PUBLIC_API_URL): upload enrichment and Gemini-written answers for a clone
that has only a Boson key.

Used only when PUBLIC_API_URL is set and no local LLM is configured (`active()`). Spoiler filtering always stays
local: answers send only passages the local retrieval already filtered. If the service is closed, unreachable or
capped, enrichment falls back to the simple local cast (with a message saying why) and typed answers report why.
"""
import asyncio
import json
import logging
import time
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Awaitable, Callable

import httpx
from pydantic import ValidationError

from . import books, gemini
from .config import settings
from .enrich import cast as cast_mod
from .enrich import equations, pipeline, series_link, textbook
from .enrich.minor import MINOR_IDS, MINOR_VOICES
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

def local_llm_configured() -> bool:
    """Claude, Vertex (GCP_PROJECT with working ADC) or an AI Studio key (GEMINI_API_KEY)."""
    return bool(settings.anthropic_api_key) or gemini.gcp_credentials_usable() or bool(settings.gemini_api_key)


def active() -> bool:
    """PUBLIC_API_URL is set and this machine has no LLM of its own."""
    return bool(settings.public_api_url.strip()) and not settings.public_api and not local_llm_configured()


def _base() -> str:
    return settings.public_api_url.strip().rstrip("/")


def _new_client(timeout=None) -> httpx.AsyncClient:
    """A client of the service (tests replace this to reach an in-process app or a mock)."""
    return httpx.AsyncClient(base_url=_base(), timeout=timeout)


def known_unavailable() -> bool:
    """The last health check or call (within PROBE_TTL) found the service closed or unreachable."""
    return _probe is not None and not _probe[1] and time.monotonic() - _probe[0] < PROBE_TTL


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
    if book.kind == "textbook":   # recap notes only; the levels tell chapters from subsections
        sections = [{"id": s.id, "title": s.title, "start_para": s.start_para, "level": max(1, min(s.level, 6))}
                    for s in book.sections]
    else:
        sections = [{"id": s.id, "title": s.title, "start_para": s.start_para} for s in book.sections]
    lang = {} if book.language == "eng" else {"language": book.language}   # #117; English sends what it always did
    return {"title": book.title, "author": book.author, "kind": book.kind, **lang, "sections": sections,
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
    stand_ins: list[Character] = []
    seen: set[str] = set()
    for raw in (result.get("characters") or [])[:cast_mod.MAIN_MAX + len(MINOR_VOICES)]:
        c = Character.model_validate(raw)
        if c.id in seen or not c.id:
            continue
        if c.id in MINOR_IDS:   # a stand-in voice for uncast speakers (enrich/minor.py): rebuilt from our own table
            v = next(v for v in MINOR_VOICES.values() if v.id == c.id)
            seen.add(c.id)
            stand_ins.append(Character(id=v.id, name=v.name, first_para=0, voice=v.voice, voice_tags=v.tags,
                                       persona="A minor speaker outside the cast.", main=False))
            continue
        if len(chars) >= cast_mod.MAIN_MAX:
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
    book.characters = chars + stand_ins
    nar = result.get("narrator") or {}
    is_char = nar.get("is_character") if nar.get("is_character") in {c.id for c in chars} else None
    voice = next((c.voice for c in chars if c.id == is_char), nar.get("voice"))
    book.narrator = Narrator(voice=voice if voice in cast_mod.VOICES else "nora", is_character=is_char)
    series, idx = result.get("series"), result.get("series_index")
    cast_mod.apply_series(book, SimpleNamespace(
        series=series if isinstance(series, str) else None,
        series_index=idx if isinstance(idx, int) and not isinstance(idx, bool) else None))

    speakers = speaker_ids(book) | {c.id for c in stand_ins}
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
            await asyncio.to_thread(apply_result, book, result)  # a whole-book rechunk: off the event loop
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
        if not note:   # stack with the same author's other uploads through the service (#134); never raises
            await series_link.reconcile(book_id, remote_llm("series"))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("public enrichment of %s failed: %s", book_id, type(e).__name__)
        report(status="error", stage="cast", message=pipeline.FAILED)
        return False


def apply_textbook_result(book: Book, result: dict) -> None:
    """Keep only notes for real sections, each ending where its section ends (the server is not trusted)."""
    ends = {t.section_id: t.end for t in textbook.targets(book)}
    out, seen = [], set()
    for s in result.get("summaries") or []:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str) or not s["text"].strip():
            continue
        sid = s.get("section_id")
        if sid not in ends or sid in seen:
            continue
        seen.add(sid)
        out.append(Summary(upto_para=ends[sid], text=" ".join(s["text"].split())[:4000], section_id=sid))
    book.summaries = sorted(out, key=lambda s: (s.upto_para, s.section_id or ""))


async def summarize_textbook(book_id: str, report: Callable[..., None], *, client: httpx.AsyncClient | None = None,
                             sleep: Callable[[float], Awaitable] = asyncio.sleep) -> bool:
    """Textbook recap notes through the public API. On failure the textbook simply has no notes. Never raises."""
    own = client is None
    try:
        book = Book.model_validate_json((settings.data_dir / "books" / book_id / "book.json").read_text(encoding="utf-8"))
        client = client or httpx.AsyncClient(base_url=_base())
        try:
            report(status="running", stage="summaries", progress=0.0, message=pipeline.TEXTBOOK_MESSAGE)
            result = await _remote_enrich(book, report, client, sleep)
        finally:
            if own:
                await client.aclose()
        apply_textbook_result(book, result)
        if not book.summaries:   # nothing usable came back: keep the notes from before
            raise ValueError("no textbook notes")
        if not await asyncio.to_thread(textbook.save_summaries, book_id, book.summaries):
            raise ValueError("book gone or no longer a textbook")
        report(status="done", stage="done", progress=1.0, message=pipeline.TEXTBOOK_DONE)
        return True
    except asyncio.CancelledError:
        raise
    except PublicFailure as e:
        log.warning("public textbook notes for %s failed: %s %s", book_id, e.kind, e)
        report(status="error", stage="summaries",
               message=pipeline.TEXTBOOK_BUDGET_USED if e.kind == "limit" else pipeline.TEXTBOOK_FAILED)
    except Exception as e:
        log.warning("public textbook notes for %s failed: %s", book_id, type(e).__name__)
        report(status="error", stage="summaries", message=pipeline.TEXTBOOK_FAILED)
    return False


# ---------- answers ----------

def answer_payload(book: Book, speaker, speaker_id: str, out: dict, query: str, spoilers: bool, history=()) -> dict:
    """The request for /api/public/answer from locally retrieved (already spoiler-filtered) passages."""
    extra = {"history": [{"role": t.role, "text": t.text} for t in history]} if history else {}
    if not settings.web_search:
        extra["web_search"] = False   # this app turned web search off; the service decides the rest (textbook/spoilers)
    if book.language != "eng":
        extra["language"] = book.language   # #117: answers default to the book's language
    return {**extra, "title": book.title, "kind": book.kind, "speaker_id": speaker_id,
            "speaker_name": speaker.name if speaker else "", "persona": speaker.persona if speaker else "",
            "story_so_far": out["story_so_far"], "query": query, "spoilers": spoilers,
            "passages": [{k: v for k, v in p.items() if k in ("book", "para_start", "para_end", "text")}
                         for p in out["passages"]]}


async def stream_answer(payload: dict, client: httpx.AsyncClient | None = None) -> AsyncIterator[dict]:
    """Yield the service's `sentence` messages (and a final `sources` one for a grounded answer). Raises PublicFailure (before any sentence for a refused request)."""
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
                    elif msg.get("type") == "sources" and isinstance(msg.get("sources"), list):
                        yield msg   # web links of a grounded answer (cleaned again by the caller)
                    elif msg.get("type") == "error":
                        raise PublicFailure("unavailable", "error line")
        except httpx.HTTPError as e:
            raise PublicFailure("unavailable", type(e).__name__) from e
    finally:
        if own:
            await client.aclose()


# ---------- small single calls: question translation, kind, title/author, face box ----------

TRANSLATE_SECONDS = 3.5   # under multilingual.TRANSLATE_TIMEOUT_S: the answer never waits on a slow service
SMALL_SECONDS = 25.0


async def _post(path: str, body: dict, timeout: float, client: httpx.AsyncClient | None = None) -> dict:
    """POST one small JSON request; returns the JSON answer. Raises PublicFailure (closed, unreachable, capped)."""
    if known_unavailable():
        raise PublicFailure("unavailable", "closed")
    own = client is None
    client = client or _new_client(httpx.Timeout(timeout, connect=min(timeout, 3.0)))
    try:
        try:
            r = await client.post(path, json=body)
        except httpx.HTTPError as e:
            raise PublicFailure("unavailable", type(e).__name__) from e
        if r.status_code != 200:
            if r.status_code in (401, 403):
                mark_unavailable()   # closed: the other calls (answers too) skip it for a minute
            raise _failure(r)
        try:
            out = r.json()
        except ValueError as e:
            raise PublicFailure("unavailable", "bad json") from e
        if not isinstance(out, dict):
            raise PublicFailure("unavailable", "bad json")
        return out
    finally:
        if own:
            await client.aclose()


async def translate(text: str, client: httpx.AsyncClient | None = None) -> str:
    """English translation of a reader's question, for searching (#12). Raises PublicFailure."""
    out = await _post("/api/public/translate", {"text": text[:500]}, TRANSLATE_SECONDS, client)
    english = out.get("text")
    if not isinstance(english, str):
        raise PublicFailure("unavailable", "bad answer")
    return english[:1000]


PASSAGE_SECONDS = 20.0


async def translate_passage(pieces: list[str], language: str, client: httpx.AsyncClient | None = None) -> list[str]:
    """A paragraph's pieces in `language` (ISO 639-3), for bilingual reading (B3). Raises PublicFailure."""
    out = await _post("/api/public/translate-passage", {"pieces": pieces, "language": language}, PASSAGE_SECONDS, client)
    got = out.get("pieces")
    if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
        raise PublicFailure("unavailable", "bad answer")
    return got


class RemoteLLM:
    """Stands in for the local LLM of enrich/detect.py ("detect") and enrich/describe.py ("describe"): the service
    runs the same fixed prompt and only the text is sent. The answer is validated with the caller's own schema."""

    def __init__(self, task: str):
        self.task = task

    async def complete_json(self, system: str, user: str, schema_model, tier: str = "fast", **_):
        out = await _post(f"/api/public/{self.task}", {"text": user}, SMALL_SECONDS)
        return schema_model.model_validate(out)


def remote_llm(task: str) -> RemoteLLM:
    return RemoteLLM(task)


async def detect_face(image: bytes, ext: str) -> dict | None:
    """The main face's box through the service, re-clamped here (the server is not trusted), or None. Never raises."""
    from . import face_detect
    try:
        out = await _post("/api/public/faces/detect", {"image": base64.b64encode(image).decode()}, SMALL_SECONDS)
        face = out.get("face")
        if not isinstance(face, dict):
            return None
        vals = [face.get(k) for k in ("x", "y", "w", "h")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            return None
        return face_detect.clamp_box(*map(float, vals))
    except PublicFailure as e:
        log.info("face detection through the service unavailable: %s", e.kind)
    except Exception as e:
        log.warning("face detection through the service failed: %s", type(e).__name__)
    return None


# ---------- equation readings (#41) ----------

EQUATIONS_UNAVAILABLE = "Equations stay as pictures (the processing service is unavailable right now)."
EQUATIONS_LIMIT = "Equations stay as pictures (the free processing limit has been reached)."
EQUATIONS_PARTIAL = "Some equations stay as pictures (the free processing limit has been reached)."
EQ_PNG_MAX = 256 * 1024          # as public_api.EQ_PNG_MAX: a bigger picture is not sent
EQ_REQUEST_BYTES = 2 * 1024 * 1024
EQ_ITEMS = 24
EQ_SECONDS = 110.0
EQ_BUSY_RETRIES = 12
EQ_BUSY_SECONDS = 5.0


def _chunks(items: list[tuple[str, bytes, str]], batch: int):
    out, size = [], 0
    for it in items:
        if out and (len(out) >= batch or size + len(it[1]) > EQ_REQUEST_BYTES):
            yield out
            out, size = [], 0
        out.append(it)
        size += len(it[1])
    if out:
        yield out


def _readings(raw, sent: set[str]) -> dict[str, dict]:
    """Keep only readings for pictures we sent, cleaned like local ones (the server is not trusted)."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in sent or not isinstance(v, dict):
            continue
        try:
            r = equations.Reading(id=k, latex=str(v.get("latex") or ""), spoken=str(v.get("spoken") or ""))
        except ValidationError:
            continue
        if clean := equations._clean(r):
            out[k] = clean
    return out


async def transcribe_equations(book_id: str, crops: dict[str, tuple[bytes, str]], inline: dict[str, tuple[bytes, str]],
                               *, stop: Callable[[], bool] = lambda: False,
                               sleep: Callable[[float], Awaitable] = asyncio.sleep) -> tuple[dict, dict, str | None]:
    """({display id: reading}, {inline id: reading}, note) through the service: a ticket first (the service estimates
    and checks its caps), then a few pictures per request, display equations first. The note is None when every
    picture was read normally, else says why (some) equations stay pictures. Never raises."""
    items = [(k, png, hint) for k, (png, hint) in list(crops.items()) + list(inline.items()) if len(png) <= EQ_PNG_MAX]
    if not items:
        return {}, {}, None
    found: dict[str, dict] = {}
    note = None
    client = _new_client(httpx.Timeout(EQ_SECONDS, connect=10.0))
    try:
        t = await _post("/api/public/equations", {"count": len(items)}, 30.0, client)
        try:
            ticket, accepted = str(t["ticket"]), int(t["accepted"])
            batch = max(1, min(int(t.get("batch") or EQ_ITEMS), EQ_ITEMS))
        except (KeyError, TypeError, ValueError):
            raise PublicFailure("unavailable", "bad ticket") from None
        if accepted < len(items):
            note = EQUATIONS_PARTIAL
        for chunk in _chunks(items[:max(0, accepted)], batch):
            if stop():
                break
            body = {"items": [{"id": k, "png": base64.b64encode(png).decode(), "hint": hint[:equations.HINT_CHARS]}
                              for k, png, hint in chunk]}
            for attempt in range(EQ_BUSY_RETRIES + 1):
                try:
                    out = await _post(f"/api/public/equations/{ticket}", body, EQ_SECONDS, client)
                    break
                except PublicFailure as e:
                    if e.kind == "limit" and str(e) in ("busy", "rate") and attempt < EQ_BUSY_RETRIES:
                        await sleep(EQ_BUSY_SECONDS)
                        continue
                    raise
            found.update(_readings(out.get("readings"), {k for k, _p, _h in chunk}))
            if out.get("stopped") in ("cap", "book_cap"):
                note = EQUATIONS_PARTIAL
                break
    except PublicFailure as e:
        log.warning("equations of %s through the service: %s %s", book_id, e.kind, e)
        if found:
            note = EQUATIONS_PARTIAL
        else:
            note = EQUATIONS_LIMIT if e.kind == "limit" else EQUATIONS_UNAVAILABLE
    except Exception as e:
        log.warning("equations of %s through the service failed: %s", book_id, type(e).__name__)
        note = EQUATIONS_PARTIAL if found else EQUATIONS_UNAVAILABLE
    finally:
        await client.aclose()
    display = {k: v for k, v in found.items() if k in crops}
    return display, {k: v for k, v in found.items() if k in inline and k not in crops}, note
