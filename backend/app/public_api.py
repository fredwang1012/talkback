"""The public processing API: a small, separate Cloud Run service (PUBLIC_API=true) that a fresh clone calls for
full upload processing (cast, dialogue split, summaries) and Gemini-written Talk answers.

Exposes ONLY GET /api/health, POST /api/public/enrich (+ GET /api/public/enrich/{job_id}) and POST /api/public/answer.
Everything else is 404 (no docs, no static frontend, no media, no book data). Gemini only.

Privacy and cost rules:
- Request bodies may hold book text: they are never logged (only counts and ids), and results are held in memory for
  30 minutes, never written to disk. The only files are spend counters (data/usage).
- Caps: per-book estimate, a persisted cumulative enrichment cap (enrich/spend.py), a persisted daily answer cap,
  per-IP rate limits and request size limits. A cap answers 402 with {"detail", "code"}; a rate limit answers 429.
- The server never decides what a reader may see: answer passages arrive already spoiler-filtered by the client.
"""
import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings
from .enrich import pipeline, spend
from .enrich.llm import SpendLimit, make_llm, price_usd
from . import emotion
from .personas import build_answer_prompt
from .ratelimit import TokenBucket, client_key, normalize_ip
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

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,80}$"

MSG_BOOK_CAP = "This book is too long for the free processing service."
MSG_TOTAL_CAP = "The free processing budget has been used up."
MSG_IP_CAP = "You have used your share of the free processing for today; try again tomorrow."
MSG_DAILY_CAP = "The free Gemini answer budget for today has been used up."


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


class EnrichRequest(BaseModel):
    """An already-ingested, narrator-only novel."""
    title: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=200)
    kind: Literal["novel"] = "novel"
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
        return Book(id="public", title=self.title, author=self.author, kind="novel", license="n/a",
                    sections=[Section(id=s.id, title=s.title, start_para=s.start_para) for s in self.sections],
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

    @field_validator("passages")
    @classmethod
    def _total(cls, v):
        if sum(len(p.text) for p in v) > 24_000:
            raise ValueError("passages too long")
        return v

    def prompt(self) -> tuple[str, str]:
        """The same prompt /api/answer builds locally, from a stub book holding only what the prompt uses."""
        chars = []
        if self.speaker_id != "narrator":
            chars = [Character(id=self.speaker_id, name=self.speaker_name or self.speaker_id, first_para=0, voice="",
                               persona=self.persona)]
        book = Book(id="public", title=self.title, author="", kind=self.kind, license="n/a", sections=[],
                    paragraphs=[], characters=chars)
        return build_answer_prompt(book, self.speaker_id, self.story_so_far, [p.model_dump() for p in self.passages],
                                   self.query, spoilers=self.spoilers, history=self.history)


# ---------- state ----------

enrich_bucket = TokenBucket(capacity=1, per_seconds=3600)    # books per IP per hour
poll_bucket = TokenBucket(capacity=120, per_seconds=60)
answer_bucket = TokenBucket(capacity=20, per_seconds=60)


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
    """Estimated spend still to come from running jobs (their estimate minus what they already spent)."""
    return sum(max(0.0, j.estimate_usd - j.usd) for j in _jobs.values() if j.status == "running")


def _result(book: Book) -> dict:
    """What the client needs back: cast, narrator, summaries, and segments only where they are not plain narration."""
    segments = {}
    for p in book.paragraphs:
        plain = len(p.segments) == 1 and p.segments[0].speaker == "narrator" and not p.segments[0].emotion
        if p.segments and not plain:
            segments[str(p.idx)] = [s.model_dump(exclude_none=True) for s in p.segments]
    return {"characters": [c.model_dump(exclude={"portrait"}) for c in book.characters],
            "narrator": book.narrator.model_dump(exclude={"portrait"}) if book.narrator else None,
            "segments": segments, "summaries": [s.model_dump() for s in book.summaries]}


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
            degraded = await pipeline.run_stages(book, job.report, llm, state=state, allow_fallback=False)
        job.result, job.degraded = _result(book), degraded or None
        job.status, job.stage, job.progress, job.message = "done", "done", 1.0, "Characters and recap are ready."
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
        log.warning("enrich job %s failed in %s: %s", job.id, state["stage"], type(e).__name__)
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
    """Bound the request body of the two POST endpoints before it is parsed (Content-Length required)."""
    LIMITS = {"/api/public/enrich": ENRICH_BODY_LIMIT, "/api/public/answer": ANSWER_BODY_LIMIT}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = self.LIMITS.get(scope.get("path")) if scope["type"] == "http" and scope["method"] == "POST" else None
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


app = FastAPI(title="Talkback public API", docs_url=None, redoc_url=None, openapi_url=None)
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
    book = req.to_book()
    est = pipeline.estimate(book, "gemini")
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
    system, user = req.prompt()
    cost = price_usd("gemini", "fast", int((len(system) + len(user)) / 3.5) + 1, MAX_OUTPUT_TOKENS)
    token = answer_budget.reserve(cost)
    if token is None:
        raise PublicError(402, "daily_cap", MSG_DAILY_CAP)
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
            actual = price_usd("gemini", "fast", state["in"], state["out"])
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
        gen = emotion.tag_sentences(get_public_answerer().stream_sentences(system, user, on_usage=on_usage))
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
