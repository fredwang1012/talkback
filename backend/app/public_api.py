"""The public processing API: a small, separate Cloud Run service (PUBLIC_API=true) that a fresh clone calls for
full upload processing (cast, dialogue split, summaries; textbooks: recap notes only) and Gemini-written Talk answers.

Exposes ONLY GET /api/health, POST /api/public/enrich (+ GET /api/public/enrich/{job_id}), POST /api/public/answer,
the small single calls POST /api/public/translate (a question for search, #12), /api/public/translate-passage (a
paragraph for bilingual reading, B3), /api/public/detect (novel or textbook),
/api/public/describe (title and author), /api/public/series (volume of a series, #134) and /api/public/faces/detect (face box, #109), and the equation readings of an
uploaded textbook (#41): POST /api/public/equations (an estimate-first ticket) then POST /api/public/equations/{ticket}
(a few pictures per request). Everything else is 404 (no docs, no static frontend, no media, no book data). Gemini only.

Privacy and cost rules:
- Request bodies may hold book text: they are never logged (only counts and ids), and results are held in memory for
  30 minutes, never written to disk. The only files are spend counters (data/usage).
- Caps: per-book estimate, a persisted cumulative enrichment cap (enrich/spend.py), a persisted daily answer cap,
  per-IP rate limits and request size limits. Equation tickets count like enrichment (per-book, total and per-IP
  caps); the small calls count on the daily answer cap, face boxes also on a persisted daily call count. A cap answers 402 with {"detail", "code"}; a rate limit answers 429.
- The server never decides what a reader may see: answer passages arrive already spoiler-filtered by the client.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings
from .enrich import describe as describe_mod
from .enrich import detect as detect_mod
from .enrich import equations, pipeline, series_early, series_link, spend, textbook
from .enrich.llm import CHARS_PER_TOKEN, SpendLimit, make_llm, price_usd
from . import face_detect, multilingual
from . import emotion
from .personas import build_answer_prompt
from .ratelimit import TokenBucket, client_key, normalize_ip
from .daily_count import DailyCount
from . import websearch
from .schema import Book, ChatTurn, Character, Paragraph, Section
from .uploadlimit import _reply

log = logging.getLogger("talkback.public")

MAX_PARAGRAPHS = 6000
MAX_PARAGRAPH_CHARS = 5000
MAX_TOTAL_CHARS = 3_000_000
MAX_SECTIONS = 2000
ENRICH_BODY_LIMIT = 3 * 1024 * 1024 + 256 * 1024     # 3 MB of text plus JSON overhead
ANSWER_BODY_LIMIT = 128 * 1024
JOB_TTL_SECONDS = 30 * 60
JOB_MAX_SECONDS = 25 * 60
MAX_RUNNING_JOBS = 2
MAX_ANSWER_STREAMS = 4
ANSWER_SLOT_SECONDS = 300      # an UNSTARTED stream slot or reservation is dropped after this (guards a leak)
ANSWER_DEADLINE_SECONDS = 120  # hard limit on one answer stream
STARTED_GRACE_SECONDS = 60     # a started reservation or slot that outlives deadline + grace is leaked: charged in full
MAX_OUTPUT_TOKENS = 700        # GeminiAnswerer max_output_tokens: the most one answer can cost in output
MAX_JOBS_KEPT = 50
GROUNDED_USD = 0.035           # one grounded prompt past Vertex's free daily tier ($35 / 1,000), on the answer budget

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,80}$"

MSG_BOOK_CAP = "This book is too long for the free processing service."
MSG_TOTAL_CAP = "The free processing budget has been used up."
MSG_IP_CAP = "You have used your share of the free processing for today; try again tomorrow."
MSG_DAILY_CAP = "The free Gemini answer budget for today has been used up."
MSG_FACE_CAP = "The free face detection for today has been used up."

# small single calls (translate, detect, describe, face box): counted on the daily answer budget
SMALL_BODY_LIMIT = 32 * 1024
TRANSLATE_MAX_CHARS = multilingual.MAX_QUERY_CHARS
SERIES_MAX_CHARS = 600
PASSAGE_MAX_PIECES = 40      # TTS chunks of one paragraph (translate.MAX_PIECES)
PASSAGE_MAX_PIECE_CHARS = 600
PASSAGE_MAX_CHARS = 8000
DETECT_MAX_CHARS = detect_mod.SAMPLE_CHARS + 200          # the sample plus its "[...]" joins
DESCRIBE_MAX_CHARS = describe_mod.MAX_CHARS
TRANSLATE_SECONDS = 4.0      # the clone's answer waits no longer (multilingual.TRANSLATE_TIMEOUT_S)
SMALL_SECONDS = 20.0
FACE_MAX_BYTES = 5 * 1024 * 1024                          # as faces.MAX_FACE_BYTES
FACE_BODY_LIMIT = FACE_MAX_BYTES * 4 // 3 + 4096
FACE_DAILY_CALLS = 100       # persisted, across every caller (~$0.0006 each)
FACE_CALLS_FILE = "public_face_calls.json"

# equation readings: a ticket (estimate first), then a few pictures per request
EQ_MAX_PICTURES = 4500       # ingest_upload.EQ_MAX_BOOK + INLINE_MAX_BOOK
EQ_ITEMS_PER_REQUEST = 24    # three Gemini batches of equations.BATCH, in parallel
EQ_PNG_MAX = 256 * 1024      # one cropped picture
EQ_REQUEST_BYTES = 2 * 1024 * 1024
EQ_BODY_LIMIT = EQ_REQUEST_BYTES * 4 // 3 + 64 * 1024
EQ_TICKET_TTL = 30 * 60      # an idle ticket is closed (and settled) after this
EQ_TICKET_START_TTL = 120    # ... or after this when no picture came yet (a held slot costs nothing to hold)
EQ_MIN_TICKET_USD = 0.02     # each ticket costs its address at least this (opening many tickets is not free)
EQ_REQUEST_SECONDS = 90.0    # under Cloud Run's 120 s request timeout
MAX_TICKETS = 32
MAX_TICKETS_PER_IP = 1
MAX_EQ_REQUESTS = 2
EQ_ID_PATTERN = r"^[em]\d{1,6}$"     # display equation / inline formula


class PublicError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# ---------- requests ----------

class PParagraph(BaseModel):
    idx: int = Field(ge=0)
    section_id: str = Field(max_length=80)
    text: str = Field(max_length=MAX_PARAGRAPH_CHARS)


class PSection(BaseModel):
    id: str = Field(max_length=80)
    title: str = Field(max_length=300)
    start_para: int = Field(ge=0)
    level: int = Field(default=1, ge=1, le=6)   # textbooks: 1 = chapter, 2 = "4.2", ...


class EnrichRequest(BaseModel):
    """An already-ingested, narrator-only novel (cast, dialogue, summaries), or a textbook (recap notes only)."""
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)
    kind: Literal["novel", "textbook"] = "novel"
    sections: list[PSection] = Field(max_length=MAX_SECTIONS)
    paragraphs: list[PParagraph] = Field(min_length=1, max_length=MAX_PARAGRAPHS)

    @model_validator(mode="after")
    def _consistent(self):
        if any(p.idx != i for i, p in enumerate(self.paragraphs)):
            raise ValueError("paragraph idx must equal its position")
        if sum(len(p.text) for p in self.paragraphs) > MAX_TOTAL_CHARS:
            raise ValueError("too much text")
        if any(s.start_para >= len(self.paragraphs) for s in self.sections):
            raise ValueError("section start_para out of range")
        return self

    def to_book(self) -> Book:
        return Book(id="public", title=self.title, author=self.author, kind=self.kind, license="n/a",
                    sections=[Section(id=s.id, title=s.title, start_para=s.start_para, level=s.level)
                              for s in self.sections],
                    paragraphs=[Paragraph(idx=p.idx, section_id=p.section_id, text=p.text) for p in self.paragraphs])


class Passage(BaseModel):
    book: str | None = Field(default=None, max_length=200)
    para_start: int = Field(ge=0)
    para_end: int = Field(ge=0)
    text: str = Field(max_length=12_000)


class AnswerRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["novel", "textbook"] = "novel"
    speaker_id: str = Field(default="narrator", pattern=ID_PATTERN)
    speaker_name: str = Field(default="", max_length=120)
    persona: str = Field(default="", max_length=2000)
    story_so_far: str = Field(default="", max_length=8000)
    passages: list[Passage] = Field(default_factory=list, max_length=12)
    query: str = Field(min_length=1, max_length=500)
    spoilers: bool = False
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)
    web_search: bool = True   # a client may turn web search off; it never turns it on (websearch.allowed decides)

    @field_validator("passages")
    @classmethod
    def _total(cls, v):
        if sum(len(p.text) for p in v) > 24_000:
            raise ValueError("passages too long")
        return v

    def prompt(self, web_search: bool = False) -> tuple[str, str]:
        """The same prompt /api/answer builds locally, from a stub book holding only what the prompt uses."""
        chars = []
        if self.speaker_id != "narrator":
            chars = [Character(id=self.speaker_id, name=self.speaker_name or self.speaker_id, first_para=0, voice="",
                               persona=self.persona)]
        book = Book(id="public", title=self.title, author="", kind=self.kind, license="n/a", sections=[],
                    paragraphs=[], characters=chars)
        return build_answer_prompt(book, self.speaker_id, self.story_so_far, [p.model_dump() for p in self.passages],
                                   self.query, spoilers=self.spoilers, history=self.history, web_search=web_search)


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=DESCRIBE_MAX_CHARS)


class Translation(BaseModel):
    english: str = Field(description="the question in English")


class PassageRequest(BaseModel):
    """One paragraph of a book for bilingual reading (B3): its TTS-chunk texts and the target language."""
    pieces: list[str] = Field(min_length=1, max_length=PASSAGE_MAX_PIECES)
    language: str = Field(min_length=3, max_length=3)

    @field_validator("pieces")
    @classmethod
    def _bounded(cls, v):
        if any(len(x) > PASSAGE_MAX_PIECE_CHARS for x in v) or sum(map(len, v)) > PASSAGE_MAX_CHARS:
            raise ValueError("passage too long")
        return v

    @field_validator("language")
    @classmethod
    def _foreign(cls, v):
        from . import tts_lang
        if not tts_lang.valid(v) or tts_lang.is_english(v):
            raise ValueError("unsupported language")
        return v


class FaceBox(BaseModel):
    found: bool
    x: float
    y: float
    w: float
    h: float


class FaceRequest(BaseModel):
    image: str = Field(min_length=1, max_length=FACE_BODY_LIMIT)

    def decoded(self) -> tuple[bytes, str]:
        data = _b64(self.image)
        mime = _image_mime(data)
        if data is None or mime is None or len(data) > FACE_MAX_BYTES:
            raise PublicError(422, "invalid", "Send a PNG, JPEG or WebP photo of at most 5 MB.")
        return data, mime


class EqStart(BaseModel):
    count: int = Field(ge=1, le=EQ_MAX_PICTURES)


class EqItem(BaseModel):
    id: str = Field(pattern=EQ_ID_PATTERN)
    png: str = Field(min_length=1, max_length=EQ_PNG_MAX * 4 // 3 + 8)
    hint: str = Field(default="", max_length=400)


class EqRequest(BaseModel):
    items: list[EqItem] = Field(min_length=1, max_length=EQ_ITEMS_PER_REQUEST)

    def decoded(self) -> list[tuple[str, bytes, str]]:
        if len({i.id for i in self.items}) != len(self.items):
            raise PublicError(422, "invalid", "Invalid request: items.id")
        out, total = [], 0
        for i in self.items:
            png = _b64(i.png)
            if png is None or not png.startswith(PNG_MAGIC) or len(png) > EQ_PNG_MAX:
                raise PublicError(422, "invalid", "Invalid request: items.png")
            total += len(png)
            out.append((i.id, png, i.hint[:equations.HINT_CHARS]))
        if total > EQ_REQUEST_BYTES:
            raise PublicError(413, "too_large", "The request is too large.")
        return out


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _b64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None


def _image_mime(data: bytes | None) -> str | None:
    """Image type from magic bytes (as faces.sniff_ext; the main app's modules are not imported here)."""
    if not data:
        return None
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------- state ----------

enrich_bucket = TokenBucket(capacity=1, per_seconds=3600)    # books per IP per hour
poll_bucket = TokenBucket(capacity=120, per_seconds=60)
answer_bucket = TokenBucket(capacity=20, per_seconds=60)
small_bucket = TokenBucket(capacity=30, per_seconds=60)      # translate / detect / describe per IP
face_bucket = TokenBucket(capacity=10, per_seconds=3600)     # face boxes per IP per hour
eq_start_bucket = TokenBucket(capacity=2, per_seconds=3600)  # equation tickets per IP per hour
eq_bucket = TokenBucket(capacity=60, per_seconds=60)         # equation requests per IP


class Job:
    def __init__(self, job_id: str, estimate_usd: float):
        self.id, self.estimate_usd = job_id, estimate_usd
        self.created = time.monotonic()
        self.finished: float | None = None   # set when the job leaves running (the result TTL counts from here)
        self.usd = 0.0                       # real spend so far (admission reserves only the unspent part of the estimate)
        self.ip = ""
        self.status = "running"          # running | done | error
        self.stage, self.progress, self.message = "queued", 0.0, "Waiting to start"
        self.code: str | None = None     # error code: cap | book_cap | failed
        self.degraded: bool | None = None
        self.result: dict | None = None
        self.task: asyncio.Task | None = None

    def report(self, **f) -> None:
        if f.get("status") == "done":
            return  # the job finishes through _run_job (the result must be attached first)
        for k in ("status", "stage", "progress", "message"):
            if k in f and f[k] is not None:
                setattr(self, k, f[k])

    def view(self) -> dict:
        out = {"status": self.status, "stage": self.stage, "progress": self.progress, "message": self.message,
               "estimate_usd": self.estimate_usd}
        if self.code:
            out["code"] = self.code
        if self.status == "done":
            out["degraded"] = self.degraded
            out["result"] = self.result
        return out


_jobs: dict[str, Job] = {}


def _purge() -> None:
    now = time.monotonic()
    for jid in [j for j, job in _jobs.items()
                if job.status != "running" and now - (job.finished or job.created) > JOB_TTL_SECONDS]:
        del _jobs[jid]
    while len(_jobs) > MAX_JOBS_KEPT:  # oldest finished first
        done = [j for j, job in _jobs.items() if job.status != "running"]
        if not done:
            break
        del _jobs[done[0]]


def _running() -> int:
    return sum(1 for j in _jobs.values() if j.status == "running")


def _reserved_usd() -> float:
    """Estimated spend still to come from running jobs and open equation tickets (estimate minus spent so far)."""
    return (sum(max(0.0, j.estimate_usd - j.usd) for j in _jobs.values() if j.status == "running")
            + sum(max(0.0, t.estimate_usd - t.usd) for t in _tickets.values()))


def _result(book: Book) -> dict:
    """What the client needs back: cast, narrator, summaries, and segments only where they are not plain narration."""
    segments = {}
    for p in book.paragraphs:
        plain = len(p.segments) == 1 and p.segments[0].speaker == "narrator" and not p.segments[0].emotion
        if p.segments and not plain:
            segments[str(p.idx)] = [s.model_dump(exclude_none=True) for s in p.segments]
    return {"characters": [c.model_dump(exclude={"portrait"}) for c in book.characters],
            "narrator": book.narrator.model_dump(exclude={"portrait"}) if book.narrator else None,
            "segments": segments, "summaries": [s.model_dump() for s in book.summaries],
            "series": book.series, "series_index": book.series_index}


async def _run_job(job: Job, book: Book, llm=None) -> None:
    llm = llm or make_llm("gemini")
    cap = settings.public_book_cap_usd * pipeline.BOOK_CAP_MARGIN

    def on_usage(provider: str, model: str, n_in: int, n_out: int, usd: float) -> None:
        job.usd = llm.usd
        total = spend.add(job.id, provider, usd, flush_now=False)
        if llm.calls % 25 == 0:
            asyncio.get_running_loop().create_task(asyncio.to_thread(spend.flush))
        if total >= settings.public_total_cap_usd:
            raise SpendLimit("budget used")
        if llm.usd > cap:
            raise SpendLimit("book cap")

    llm.on_usage = on_usage
    state = {"stage": "cast"}
    try:
        async with asyncio.timeout(JOB_MAX_SECONDS):
            if book.kind == "textbook":   # recap notes only (enrich/textbook.py)
                _, subs = textbook.plan(book, "gemini", cap=settings.public_book_cap_usd)
                book.summaries = await pipeline.run_textbook_stage(book, job.report, llm, state=state,
                                                                   include_subsections=subs)
                if not book.summaries:   # every section call failed: not a result
                    raise RuntimeError("no textbook notes")
                degraded, done_msg = False, pipeline.TEXTBOOK_DONE
            else:
                degraded = await pipeline.run_stages(book, job.report, llm, state=state, allow_fallback=False)
                done_msg = "Characters and recap are ready."
        job.result, job.degraded = _result(book), degraded or None
        job.status, job.stage, job.progress, job.message = "done", "done", 1.0, done_msg
        log.info("enrich job %s done: $%.4f in %d calls", job.id, llm.usd, llm.calls)
    except SpendLimit as e:
        job.status, job.code = "error", "cap" if "budget" in str(e) else "book_cap"
        job.message = MSG_TOTAL_CAP if job.code == "cap" else MSG_BOOK_CAP
        log.warning("enrich job %s stopped in %s: %s", job.id, state["stage"], e)
    except asyncio.CancelledError:
        job.status, job.code, job.message = "error", "failed", "The job was cancelled."
        raise
    except Exception as e:  # never leak details (they may quote the book)
        job.status, job.code, job.message = "error", "failed", "Processing failed."
        log.warning("enrich job %s failed in %s: %s at %s", job.id, state["stage"], type(e).__name__, pipeline.where(e))
    finally:
        job.finished = time.monotonic()
        job.usd = llm.usd
        ip_budget.settle(job.ip, job.estimate_usd, llm.usd)
        await asyncio.shield(asyncio.to_thread(spend.flush))


# ---------- answer budget (persisted per UTC day) ----------

class DailyBudget:
    """Gemini answer spend of the current UTC day in data/usage/public_answer_spend.json (written atomically).

    `reserve` is the admission check: it counts what in-flight answers may still cost, so concurrent requests cannot
    all slip under the cap, and returns a token. `settle(token, actual)` replaces the reservation by the real cost.
    A reservation nobody settles (the response body never started) expires after ANSWER_SLOT_SECONDS and is released
    without a charge: no Gemini call was made for it.
    """
    FILE = "public_answer_spend.json"

    def __init__(self, clock=time.monotonic):
        self.day, self.usd, self.path = None, 0.0, None
        self.clock = clock
        self._held: dict[str, tuple[float, float]] = {}    # token -> (usd, taken at)
        self._started: dict[str, float] = {}               # tokens whose stream is running -> start time
        self.lock = threading.Lock()

    @property
    def reserved(self) -> float:
        return sum(v[0] for v in self._held.values())

    def _sync(self) -> None:
        path = settings.data_dir / "usage" / self.FILE
        today = datetime.now(timezone.utc).date().isoformat()
        if self.path != path:
            self.path, self.day, self.usd = path, None, 0.0
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.day, self.usd = data.get("day"), float(data.get("usd", 0.0))
            except (OSError, ValueError, TypeError):
                pass
        if self.day != today:
            self.day, self.usd = today, 0.0
        now = self.clock()
        changed = False
        for t, (usd, at) in list(self._held.items()):
            if t in self._started:
                if now - self._started[t] > ANSWER_DEADLINE_SECONDS + STARTED_GRACE_SECONDS:
                    # leaked while running (its settle never came): count the full reservation as spent, never drop it
                    del self._held[t]
                    del self._started[t]
                    self.usd = round(self.usd + usd, 6)
                    changed = True
            elif now - at > ANSWER_SLOT_SECONDS:
                del self._held[t]           # never started: no model call was made, no charge
        if changed:
            spend._write({"day": self.day, "usd": self.usd}, self.path)

    def spent(self) -> float:
        with self.lock:
            self._sync()
            return self.usd

    def reserve(self, usd: float) -> str | None:
        with self.lock:
            self._sync()
            if self.usd + self.reserved + usd > settings.public_answer_daily_cap_usd:
                return None
            token = secrets.token_hex(8)
            self._held[token] = (usd, self.clock())
            return token

    def mark_started(self, token: str) -> None:
        with self.lock:
            if token in self._held:
                self._started[token] = self.clock()

    def settle(self, token: str, actual: float) -> None:
        """Release the reservation and charge the real cost. Idempotent: a token that is no longer held (settled
        before, or expired and already charged in full) charges nothing."""
        with self.lock:
            self._sync()
            if self._held.pop(token, None) is None:
                return
            self._started.pop(token, None)
            self.usd = round(self.usd + actual, 6)
            spend._write({"day": self.day, "usd": self.usd}, self.path)


class IpBudget:
    """Enrichment dollars per client address per UTC day (addresses stored as short hashes), persisted.

    The estimate is charged at admission and settled to the real spend when the job ends.
    """
    FILE = "public_ip_spend.json"

    def __init__(self):
        self.day, self.ips, self.path = None, {}, None
        self.lock = threading.Lock()

    def _sync(self) -> None:
        path = settings.data_dir / "usage" / self.FILE
        today = datetime.now(timezone.utc).date().isoformat()
        if self.path != path:
            self.path, self.day, self.ips = path, None, {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.day, self.ips = data.get("day"), dict(data.get("ips", {}))
            except (OSError, ValueError, TypeError):
                pass
        if self.day != today:
            self.day, self.ips = today, {}

    @staticmethod
    def _key(ip: str) -> str:
        return hashlib.sha256(normalize_ip(ip).encode()).hexdigest()[:16]

    def spent(self, ip: str) -> float:
        with self.lock:
            self._sync()
            return float(self.ips.get(self._key(ip), 0.0))

    def charge(self, ip: str, usd: float) -> bool:
        """Charge `usd` unless it would take this address over its daily cap."""
        with self.lock:
            self._sync()
            k = self._key(ip)
            if self.ips.get(k, 0.0) + usd > settings.public_ip_daily_cap_usd:
                return False
            self.ips[k] = round(self.ips.get(k, 0.0) + usd, 6)
            spend._write({"day": self.day, "ips": self.ips}, self.path)
            return True

    def settle(self, ip: str, estimate: float, actual: float) -> None:
        with self.lock:
            self._sync()
            k = self._key(ip)
            if k in self.ips:
                self.ips[k] = round(max(0.0, self.ips[k] - estimate + actual), 6)
                spend._write({"day": self.day, "ips": self.ips}, self.path)


ip_budget = IpBudget()


face_calls = DailyCount(FACE_CALLS_FILE)
grounded_calls = DailyCount(websearch.PUBLIC_FILE)   # grounded answers per UTC day (settings.public_grounded_daily_cap)


class Ticket:
    """An admitted equation-reading run: `remaining` pictures may still be sent, `usd` is the real spend so far."""

    def __init__(self, ticket_id: str, ip: str, accepted: int, estimate_usd: float):
        self.id, self.ip, self.remaining, self.estimate_usd = ticket_id, ip, accepted, estimate_usd
        self.usd = 0.0
        self.used = time.monotonic()
        self.started = False   # a picture request came
        self.busy = False      # a request is running on it: one at a time, never settled meanwhile


_tickets: dict[str, Ticket] = {}
_eq_running = 0


def _close_ticket(t: Ticket) -> None:
    """Settle the caller's daily cap from the estimate to the real spend, at least EQ_MIN_TICKET_USD (once). Never
    while a request runs on the ticket (its spend is not final yet)."""
    if t.busy:
        return
    if _tickets.pop(t.id, None) is not None:
        ip_budget.settle(t.ip, t.estimate_usd, max(t.usd, EQ_MIN_TICKET_USD))


def _purge_tickets() -> None:
    now = time.monotonic()
    for t in list(_tickets.values()):
        ttl = EQ_TICKET_TTL if t.started else EQ_TICKET_START_TTL
        if not t.busy and now - t.used > ttl:
            _close_ticket(t)


def _eq_fit(count: int) -> int:
    """How many of `count` pictures fit under the per-book cap (the client sends display equations first)."""
    n = count
    while n and equations.estimate(n) > settings.public_book_cap_usd:
        n = n * 9 // 10
    return n
_streams: dict[str, float] = {}   # answer stream slots: token -> start time
_started_slots: dict[str, float] = {}  # slots whose stream is running -> start time


def _take_stream_slot() -> str | None:
    now = time.monotonic()
    for t, at in list(_streams.items()):
        started = _started_slots.get(t)
        if (started is None and now - at > ANSWER_SLOT_SECONDS) or (
                started is not None and now - started > ANSWER_DEADLINE_SECONDS + STARTED_GRACE_SECONDS):
            del _streams[t]
            _started_slots.pop(t, None)
    if len(_streams) >= MAX_ANSWER_STREAMS:
        return None
    token = secrets.token_hex(8)
    _streams[token] = now
    return token


answer_budget = DailyBudget()


@lru_cache(maxsize=1)
def get_public_answerer():
    """Lazy Gemini answerer; tests monkeypatch this to avoid Vertex."""
    from .gcp.llm import GeminiAnswerer
    return GeminiAnswerer()


# ---------- app ----------

class BodyLimit:
    """Bound the request body of every POST endpoint before it is parsed (Content-Length required)."""
    LIMITS = {"/api/public/enrich": ENRICH_BODY_LIMIT, "/api/public/answer": ANSWER_BODY_LIMIT,
              "/api/public/translate": SMALL_BODY_LIMIT, "/api/public/detect": SMALL_BODY_LIMIT,
              "/api/public/translate-passage": SMALL_BODY_LIMIT,
              "/api/public/describe": SMALL_BODY_LIMIT, "/api/public/series": SMALL_BODY_LIMIT,
              "/api/public/series-early": SMALL_BODY_LIMIT,
              "/api/public/faces/detect": FACE_BODY_LIMIT,
              "/api/public/equations": 1024}

    def __init__(self, app):
        self.app = app

    @classmethod
    def limit(cls, path: str) -> int | None:
        if path in cls.LIMITS:
            return cls.LIMITS[path]
        return EQ_BODY_LIMIT if path.startswith("/api/public/equations/") else None

    async def __call__(self, scope, receive, send):
        limit = self.limit(scope.get("path", "")) if scope["type"] == "http" and scope["method"] == "POST" else None
        if limit is None:
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        declared = headers.get(b"content-length")
        if b"transfer-encoding" in headers or declared is None:
            return await _reply(send, 411, "Content-Length is required.")
        if not (declared.isascii() and declared.isdigit()) or len(declared) > 12:
            return await _reply(send, 400, "Invalid Content-Length.")
        if int(declared) > limit:
            return await _reply(send, 413, "The request is too large.")
        received = 0
        exceeded = responded = False

        async def counting_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal responded
            if exceeded:
                return
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not responded:
            await _reply(send, 413, "The request is too large.")


async def _note_activity(request: Request) -> None:
    """Who uses the public API (by client IP; it has no accounts): see app/activity.py."""
    from . import activity
    from .ratelimit import client_ip
    activity.note(f"ip:{client_ip(request)}", request.method, activity.route_of(request))


app = FastAPI(title="Talkback public API", docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(_note_activity)])
app.add_middleware(BodyLimit)


@app.exception_handler(PublicError)
async def _public_error(_: Request, e: PublicError):
    return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _invalid(_: Request, e: RequestValidationError):
    # the default body echoes the input (book text); say only which fields are wrong
    where = sorted({".".join(str(x) for x in err["loc"][1:3]) for err in e.errors()})[:5]
    return JSONResponse({"detail": "Invalid request: " + ", ".join(where), "code": "invalid"}, status_code=422)


@app.get("/api/health")
def health():
    return {"ok": True, "public": True}


@app.post("/api/public/enrich", status_code=202)
async def start_enrich(req: EnrichRequest, request: Request):
    if not enrich_bucket.allow(client_key(request)):
        raise PublicError(429, "rate", "Too many books from your address; try again later.")
    _purge()
    _purge_tickets()   # an abandoned equations ticket must not keep its reservation (and the caller's charge)
    book = req.to_book()
    est = (textbook.plan(book, "gemini", cap=settings.public_book_cap_usd)[0] if book.kind == "textbook"
           else pipeline.estimate(book, "gemini"))
    if est > settings.public_book_cap_usd:
        raise PublicError(402, "book_cap", MSG_BOOK_CAP)
    if spend.total_usd() + _reserved_usd() + est > settings.public_total_cap_usd:
        raise PublicError(402, "cap", MSG_TOTAL_CAP)
    if _running() >= MAX_RUNNING_JOBS:
        raise PublicError(429, "busy", "The processing service is busy; try again in a minute.")
    ip = client_key(request)
    if not ip_budget.charge(ip, est):
        raise PublicError(402, "ip_cap", MSG_IP_CAP)
    job = Job(secrets.token_urlsafe(16), round(est, 4))
    job.ip = ip
    _jobs[job.id] = job
    job.task = asyncio.create_task(_run_job(job, book))
    log.info("enrich job %s started: %d paragraphs, estimate $%.4f", job.id, len(book.paragraphs), est)
    return {"job_id": job.id, "estimate_usd": job.estimate_usd}


@app.get("/api/public/enrich/{job_id}")
async def enrich_status(job_id: str, request: Request):
    if not poll_bucket.allow(client_key(request)):
        raise PublicError(429, "rate", "Polling too fast.")
    _purge()
    job = _jobs.get(job_id)
    if job is None:
        raise PublicError(404, "unknown_job", "Unknown or expired job.")
    return job.view()


@app.post("/api/public/answer")
async def public_answer(req: AnswerRequest, request: Request):
    if not answer_bucket.allow(client_key(request)):
        raise PublicError(429, "rate", "Too many questions; wait a moment.")
    # web search: textbooks and spoilers-on novels only (never a spoiler-safe novel), when the budget has room for
    # its fee and today's grounded sub-cap is not used up; otherwise the plain answer
    grounded, token = False, None
    if req.web_search and websearch.allowed(req.kind, req.spoilers):
        system, user = req.prompt(web_search=True)
        cost = price_usd("gemini", "fast", int((len(system) + len(user)) / 3.5) + 1, MAX_OUTPUT_TOKENS, model=settings.gemini_model)
        token = answer_budget.reserve(cost + GROUNDED_USD)
        if token is not None and not grounded_calls.take(settings.public_grounded_daily_cap):
            answer_budget.settle(token, 0.0)
            token = None
        grounded = token is not None
    if not grounded:
        system, user = req.prompt()
        cost = price_usd("gemini", "fast", int((len(system) + len(user)) / 3.5) + 1, MAX_OUTPUT_TOKENS, model=settings.gemini_model)
        token = answer_budget.reserve(cost)
    if token is None:
        raise PublicError(402, "daily_cap", MSG_DAILY_CAP)
    if grounded:
        cost += GROUNDED_USD   # the worst case, settled when no usage arrives
    sources: list[dict] = []
    suggestions: list[str] = []
    slot = _take_stream_slot()
    if slot is None:
        answer_budget.settle(token, 0.0)
        raise PublicError(429, "busy", "Too many answers in progress; try again in a moment.")
    state = {"in": 0, "out": 0, "seen": False, "started": False, "done": False}

    def finish() -> None:
        """Settle once: the real cost when usage arrived, the worst case if the model ran without reporting, nothing
        if the body never started. Also reached from the response's background task and, failing that, by expiry."""
        if state["done"]:
            return
        state["done"] = True
        if state["seen"]:
            actual = price_usd("gemini", "fast", state["in"], state["out"], model=settings.gemini_model) + (GROUNDED_USD if sources else 0.0)
        else:
            actual = cost if state["started"] else 0.0
        answer_budget.settle(token, actual)
        _streams.pop(slot, None)
        _started_slots.pop(slot, None)

    def on_usage(n_in: int, n_out: int) -> None:
        state.update({"in": n_in, "out": n_out, "seen": True})

    async def stream():
        state["started"] = True
        answer_budget.mark_started(token)
        _started_slots[slot] = time.monotonic()
        deadline = asyncio.get_running_loop().time() + ANSWER_DEADLINE_SECONDS
        search_kw = ({"web_search": True, "on_sources": sources.extend, "on_suggestions": suggestions.append}
                     if grounded else {})
        gen = emotion.tag_sentences(get_public_answerer().stream_sentences(system, user, on_usage=on_usage, **search_kw))
        try:
            try:
                it = gen.__aiter__()
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    try:
                        async with asyncio.timeout(max(remaining, 0.0)):
                            sentence, feeling = await it.__anext__()
                    except StopAsyncIteration:
                        break
                    yield json.dumps(emotion.sentence_line(sentence, feeling), ensure_ascii=False) + "\n"
                    if deadline - asyncio.get_running_loop().time() <= 0:
                        raise TimeoutError
            except TimeoutError:
                log.warning("public answer cut at its deadline")
                yield json.dumps({"type": "error", "message": "The answer took too long; please try again."}) + "\n"
                return
            except Exception as e:  # never leak details (they may quote the book)
                log.warning("public answer failed: %s", type(e).__name__)
                yield json.dumps({"type": "error", "message": "The answer could not be generated; please try again."}) + "\n"
                return
            event = websearch.sources_event(sources, suggestions[-1] if suggestions else None)
            if event:  # web links + Google's Search Suggestions of a grounded answer, shown under it (never spoken)
                yield json.dumps(event, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done"}) + "\n"
        finally:
            try:
                async with asyncio.timeout(5):
                    await gen.aclose()  # runs the answerer's own finally: that is where the token counts arrive
            except BaseException:
                pass
            finally:
                finish()

    try:
        return StreamingResponse(stream(), media_type="application/x-ndjson", background=BackgroundTask(finish))
    except BaseException:
        finish()
        raise


# ---------- small single calls: translate, detect, describe, face box ----------

async def _single_call(system: str, user, schema, *, in_chars: float, max_out: int, seconds: float):
    """One Gemini call counted on the daily answer budget: the worst case is reserved first and the real cost settled
    after (the worst case when the call failed without reporting usage). Raises PublicError; never leaks details."""
    worst = price_usd("gemini", "fast", int(in_chars / CHARS_PER_TOKEN) + 1, max_out)
    token = answer_budget.reserve(worst)
    if token is None:
        raise PublicError(402, "daily_cap", MSG_DAILY_CAP)
    llm = make_llm("gemini")
    try:
        async with asyncio.timeout(seconds):
            return await llm.complete_json(system, user, schema, "fast", max_tokens=max_out)
    except TimeoutError:
        log.warning("public %s call timed out", schema.__name__)
        raise PublicError(504, "timeout", "The model took too long; try again.") from None
    except Exception as e:
        log.warning("public %s call failed: %s", schema.__name__, type(e).__name__)
        raise PublicError(502, "failed", "The model call failed; try again.") from None
    finally:
        answer_budget.settle(token, llm.usd if llm.calls else worst)


def _small_allowed(request: Request) -> None:
    if not small_bucket.allow(client_key(request)):
        raise PublicError(429, "rate", "Too many requests; wait a moment.")


@app.post("/api/public/translate")
async def public_translate(req: TextRequest, request: Request):
    """English for searching the English book (#12). A question that is not clearly foreign comes back as it is, with
    no model call."""
    if len(req.text) > TRANSLATE_MAX_CHARS:
        raise PublicError(422, "invalid", "Invalid request: text")
    _small_allowed(request)
    text = req.text.strip()
    if multilingual.foreign_language(text) is None:
        return {"text": req.text}
    system = multilingual.TRANSLATE_SYSTEM + " Answer as JSON with the field english."
    out = await _single_call(system, text, Translation, in_chars=len(system) + len(text), max_out=200,
                             seconds=TRANSLATE_SECONDS)
    return {"text": " ".join(out.english.split())[:TRANSLATE_MAX_CHARS * 2]}


@app.post("/api/public/translate-passage")
async def public_translate_passage(req: PassageRequest, request: Request):
    """A paragraph translated piece by piece for bilingual reading (B3), with translate.py's own prompt. Counted on
    the daily answer budget like the other small calls; the clone caches the result on its disk."""
    from . import translate
    _small_allowed(request)
    system = translate.system_prompt(translate.language_name(req.language))
    user = json.dumps(req.pieces, ensure_ascii=False)
    chars = sum(map(len, req.pieces))
    out = await _single_call(system, user, translate.Pieces, in_chars=len(system) + len(user),
                             max_out=200 + 3 * chars, seconds=SMALL_SECONDS)
    return {"pieces": [p[:PASSAGE_MAX_PIECE_CHARS * 3] for p in out.pieces]}


@app.post("/api/public/detect")
async def public_detect(req: TextRequest, request: Request):
    """Novel or textbook from a short sample (enrich/detect.py's prompt); the clone still applies its heuristic check."""
    if len(req.text) > DETECT_MAX_CHARS:
        raise PublicError(422, "invalid", "Invalid request: text")
    _small_allowed(request)
    out = await _single_call(detect_mod.SYSTEM, req.text, detect_mod.KindGuess,
                             in_chars=len(detect_mod.SYSTEM) + len(req.text), max_out=200, seconds=SMALL_SECONDS)
    return out.model_dump()


@app.post("/api/public/describe")
async def public_describe(req: TextRequest, request: Request):
    """Title and authors from a book's opening pages (enrich/describe.py's prompt); the clone cleans the answer."""
    _small_allowed(request)
    out = await _single_call(describe_mod.SYSTEM, req.text, describe_mod.Guess,
                             in_chars=len(describe_mod.SYSTEM) + len(req.text), max_out=describe_mod.OUT_TOKENS,
                             seconds=SMALL_SECONDS)
    return out.model_dump()


@app.post("/api/public/series")
async def public_series(req: TextRequest, request: Request):
    """Is this book a volume of that series, and which number (enrich/series_link.py's prompt; #134). Text only."""
    if len(req.text) > SERIES_MAX_CHARS:
        raise PublicError(422, "invalid", "Invalid request: text")
    _small_allowed(request)
    out = await _single_call(series_link.SYSTEM, req.text, series_link.SeriesGuess,
                             in_chars=len(series_link.SYSTEM) + len(req.text), max_out=200, seconds=SMALL_SECONDS)
    return out.model_dump()


@app.post("/api/public/series-early")
async def public_series_early(req: TextRequest, request: Request):
    """Series name and volume number from a new upload's title, author and opening text (enrich/series_early.py)."""
    if len(req.text) > series_early.MAX_REQUEST_CHARS:
        raise PublicError(422, "invalid", "Invalid request: text")
    _small_allowed(request)
    out = await _single_call(series_early.SYSTEM, req.text, series_early.EarlyGuess,
                             in_chars=len(series_early.SYSTEM) + len(req.text), max_out=series_early.OUT_TOKENS * 5,
                             seconds=SMALL_SECONDS)
    return out.model_dump()


def image_part(data: bytes, mime: str):
    from google.genai import types
    return types.Part.from_bytes(data=data, mime_type=mime)


@app.post("/api/public/faces/detect")
async def public_face_detect(req: FaceRequest, request: Request):
    """The main face's box as fractions of the photo, or null (face_detect.py's prompt). Nothing is stored."""
    data, mime = req.decoded()
    if not face_bucket.allow(client_key(request)):
        raise PublicError(429, "rate", "Too many photos; place the square yourself.")
    if not face_calls.take(FACE_DAILY_CALLS):
        raise PublicError(402, "face_cap", MSG_FACE_CAP)
    box = await _single_call(face_detect.PROMPT, [image_part(data, mime)], FaceBox,
                             in_chars=face_detect.IMAGE_TOKENS * CHARS_PER_TOKEN + len(face_detect.PROMPT),
                             max_out=face_detect.OUT_TOKENS, seconds=SMALL_SECONDS)
    return {"face": face_detect.clamp_box(box.x, box.y, box.w, box.h) if box.found else None}


# ---------- equation readings (#41) ----------

@app.post("/api/public/equations")
async def start_equations(req: EqStart, request: Request):
    """Admit an equation-reading run: the estimate for the pictures that fit under the per-book cap is checked against
    the total cap and charged to the caller's daily cap. The answer says how many pictures the ticket accepts."""
    ip = client_key(request)
    if not eq_start_bucket.allow(ip):
        raise PublicError(429, "rate", "Too many textbooks from your address; try again later.")
    _purge()
    _purge_tickets()
    accepted = _eq_fit(req.count)
    if accepted == 0:
        raise PublicError(402, "book_cap", MSG_BOOK_CAP)
    est = max(equations.estimate(accepted), EQ_MIN_TICKET_USD)
    if spend.total_usd() + _reserved_usd() + est > settings.public_total_cap_usd:
        raise PublicError(402, "cap", MSG_TOTAL_CAP)
    if sum(1 for t in _tickets.values() if t.ip == ip) >= MAX_TICKETS_PER_IP:
        raise PublicError(429, "ticket_open", "Your address already has equations in progress; try again later.")
    if len(_tickets) >= MAX_TICKETS:
        raise PublicError(429, "busy", "The processing service is busy; try again in a minute.")
    if not ip_budget.charge(ip, est):
        raise PublicError(402, "ip_cap", MSG_IP_CAP)
    t = Ticket(secrets.token_urlsafe(16), ip, accepted, est)
    _tickets[t.id] = t
    log.info("equation ticket %s: %d of %d pictures, estimate $%.4f", t.id, accepted, req.count, est)
    return {"ticket": t.id, "accepted": accepted, "estimate_usd": est, "batch": EQ_ITEMS_PER_REQUEST}


@app.post("/api/public/equations/{ticket}")
async def read_equations(ticket: str, req: EqRequest, request: Request):
    """LaTeX and a spoken reading for up to EQ_ITEMS_PER_REQUEST pictures of an open ticket. A cap reached on the way
    stops the run ("stopped": cap | book_cap) and keeps what was read; the ticket closes once all its pictures came."""
    global _eq_running
    ip = client_key(request)
    if not eq_bucket.allow(ip):
        raise PublicError(429, "rate", "Too many requests; wait a moment.")
    _purge_tickets()
    t = _tickets.get(ticket)
    if t is None or t.ip != ip:
        raise PublicError(404, "unknown_ticket", "Unknown or expired ticket.")
    items = req.decoded()
    if len(items) > t.remaining:
        raise PublicError(422, "too_many", "More pictures than the ticket accepted.")
    if t.busy or _eq_running >= MAX_EQ_REQUESTS:
        raise PublicError(429, "busy", "The processing service is busy; try again in a moment.")
    # from here to the first await nothing yields: the checks above and these updates are atomic on the event loop
    t.busy, t.started = True, True
    _eq_running += 1
    t.remaining -= len(items)
    t.used = time.monotonic()
    llm = make_llm("gemini")
    cap = settings.public_book_cap_usd * pipeline.BOOK_CAP_MARGIN

    def on_usage(provider: str, model: str, n_in: int, n_out: int, usd: float) -> None:
        t.usd += usd          # additive: no request can overwrite another's spend
        total = spend.add(t.id, provider, usd, flush_now=False)
        if total >= settings.public_total_cap_usd:
            raise SpendLimit("budget used")
        if t.usd > cap:
            raise SpendLimit("book cap")

    llm.on_usage = on_usage
    found: dict[str, dict] = {}
    stopped = None
    try:
        async with asyncio.timeout(EQ_REQUEST_SECONDS):
            await equations.read(items, llm, found)
    except SpendLimit as e:
        stopped = "cap" if "budget" in str(e) else "book_cap"
    except TimeoutError:
        stopped = "timeout"
    except Exception as e:  # never leak details (they may quote the book)
        log.warning("equation request on %s failed: %s", t.id, type(e).__name__)
    finally:
        _eq_running -= 1
        t.busy = False
        t.used = time.monotonic()
        if stopped in ("cap", "book_cap") or t.remaining <= 0:
            _close_ticket(t)
        await asyncio.shield(asyncio.to_thread(spend.flush))
    log.info("equation ticket %s: %d of %d read, $%.4f%s", t.id, len(found), len(items), llm.usd,
             f" (stopped: {stopped})" if stopped else "")
    return {"readings": found, **({"stopped": stopped} if stopped else {})}
