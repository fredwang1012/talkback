"""One adaptive concurrency limit for every Gemini call in the process (enrichment, answers, translation, face
detection, public jobs), so parallel uploads never stampede the project's per-minute quota.

Vertex answers a quota hit with google.genai ClientError, HTTP 429, status RESOURCE_EXHAUSTED (confirmed live); it sends
no Retry-After, but one is honoured when present (header or RetryInfo.retryDelay). The limit is AIMD: it starts at
settings.gemini_concurrency_start, grows by one after GROW_AFTER consecutive successes up to gemini_concurrency_max, and
halves on a 429 (once per generation: the 429s of calls that were already in flight at the cut are the same overload),
never below 1. Callers over the limit queue instead of failing. Interactive calls (Talk answers, translation, a face
upload) are served before background work and keep RESERVED slots that background calls may not take, so typing a
question never waits behind a book's batch. `call()` adds the retries: patient for background work (6 attempts, backoff
to 15-30 s with jitter, about a minute in all) and short for interactive calls (retrying's defaults); a retry sleeps without holding a slot. Every
~30 s while throttled an info line gives the limit, the queue and the 429s of the last minute."""
import asyncio
import logging
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Literal, TypeVar

from . import retrying
from .config import settings

log = logging.getLogger("talkback.pacer")
T = TypeVar("T")
Priority = Literal["interactive", "background"]

GROW_AFTER = 10          # consecutive successes per +1
RESERVED = 1             # slots background work leaves to interactive calls
DEBUG_EVERY_S = 30.0
RATE_LIMIT_NAMES = ("TooManyRequests", "ResourceExhausted")
BACKGROUND = dict(attempts=6, base=2.0, cap=30.0)   # sleeps of ~1-2, 2-4, 4-8, 8-16, 15-30 s: about a minute of patience
INTERACTIVE = dict(attempts=retrying.ATTEMPTS, base=retrying.BASE_S, cap=retrying.CAP_S)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def is_rate_limited(exc: BaseException) -> bool:
    for e in retrying._chain(exc):
        if retrying.status_of(e) == 429 or type(e).__name__ in RATE_LIMIT_NAMES:
            return True
    return False


def retry_after_s(exc: BaseException) -> float | None:
    """The server's wait hint in seconds: a Retry-After header, or google.rpc.RetryInfo.retryDelay ("2.5s") in the
    error details. None when there is none (Vertex sends none today)."""
    for e in retrying._chain(exc):
        headers = getattr(getattr(e, "response", None), "headers", None)
        try:
            raw = headers.get("retry-after") if headers is not None else None
        except Exception:
            raw = None
        if raw and (m := _DURATION.match(str(raw))):
            return float(m.group(1))
        details = getattr(e, "details", None)
        err = details.get("error", details) if isinstance(details, dict) else None
        for d in (err or {}).get("details", []) if isinstance(err, dict) else []:
            delay = d.get("retryDelay") if isinstance(d, dict) else None
            if delay and (m := _DURATION.match(str(delay))):
                return float(m.group(1))
    return None


class Slot:
    """One granted call. `ok()` / `throttled(exc)` report how it went; `release()` (idempotent) gives the slot back."""

    def __init__(self, pacer: "Pacer", generation: int):
        self._pacer, self.generation, self._released = pacer, generation, False

    def ok(self) -> None:
        self._pacer._success()

    def throttled(self, exc: BaseException) -> None:
        self._pacer._rate_limited(self, exc)

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._pacer._release()


class Pacer:
    def __init__(self, *, start: int | None = None, floor: int = 1, ceiling: int | None = None,
                 grow_after: int = GROW_AFTER, reserved: int = RESERVED,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.floor = max(1, floor)
        self.ceiling = max(self.floor, ceiling if ceiling is not None else settings.gemini_concurrency_max)
        self.start = min(self.ceiling, max(self.floor, start if start is not None else settings.gemini_concurrency_start))
        self.grow_after, self.reserved = max(1, grow_after), max(0, reserved)
        self._clock, self._sleep = clock, sleep
        self._interactive: deque[asyncio.Future] = deque()
        self._background: deque[asyncio.Future] = deque()
        self._wake: asyncio.Task | None = None
        self.reset()

    def reset(self) -> None:
        self.limit = self.start
        self.low_water = self.start          # the lowest the limit has been (tests, debugging)
        self.in_flight = 0
        self.generation = 0
        self._successes = 0
        self.hold_until = 0.0
        self._hits: deque[float] = deque()   # times of the 429s of the last minute
        self._last_debug = float("-inf")

    # ---- state ----

    def queued(self) -> int:
        return len(self._interactive) + len(self._background)

    def recent_429(self) -> int:
        now = self._clock()
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        return len(self._hits)

    def _cap(self, priority: str) -> int:
        bg = max(1, self.limit - self.reserved)
        return bg if priority == "background" else max(self.limit, bg + self.reserved)

    def _room(self, priority: str) -> bool:
        return self.in_flight < self._cap(priority) and self._clock() >= self.hold_until

    # ---- acquiring ----

    async def acquire(self, priority: Priority = "background") -> Slot:
        """A slot, at once when there is room (and nobody of the same or higher priority waits), else after queueing."""
        q = self._interactive if priority == "interactive" else self._background
        ahead = bool(self._interactive) or (priority == "background" and bool(self._background))
        if not ahead and self._room(priority):
            self.in_flight += 1
            self._maybe_debug()
            return Slot(self, self.generation)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        q.append(fut)
        self._maybe_debug()
        self._schedule_wake()
        try:
            return await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                fut.result().release()   # granted in the same tick as the cancellation: back to the pool
            else:
                try:
                    q.remove(fut)
                except ValueError:
                    pass
            raise

    @asynccontextmanager
    async def slot(self, priority: Priority = "background"):
        s = await self.acquire(priority)
        try:
            yield s
        finally:
            s.release()

    async def call(self, fn: Callable[[], Awaitable[T]], *, priority: Priority = "background", what: str = "gemini call",
                   attempts: int | None = None, base: float | None = None, cap: float | None = None,
                   sleep: Callable[[float], Awaitable[None]] | None = None,
                   retry_if: Callable[[BaseException], bool] = retrying.is_transient) -> T:
        """`await fn()` under a slot, with retries of transient errors (429, 5xx, network) on the lane's budget; each
        attempt takes a slot and gives it back before the backoff sleep. A 429 shrinks the shared limit."""
        policy = INTERACTIVE if priority == "interactive" else BACKGROUND

        async def guarded() -> T:
            async with self.slot(priority) as s:
                try:
                    out = await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if is_rate_limited(e) and not retrying.is_daily_quota(e):   # a spent day's quota is final, not load
                        s.throttled(e)
                    raise
                s.ok()
                return out
        return await retrying.retry_async(
            guarded, what=what, attempts=attempts or policy["attempts"], base=base if base is not None else policy["base"],
            cap=cap if cap is not None else policy["cap"], sleep=sleep, retry_if=retry_if)

    # ---- feedback ----

    def _success(self) -> None:
        self._successes += 1
        if self._successes >= self.grow_after:
            self._successes = 0
            if self.limit < self.ceiling:
                self.limit += 1
                self._dispatch()

    def _rate_limited(self, slot: Slot, exc: BaseException) -> None:
        now = self._clock()
        self._hits.append(now)
        self._successes = 0
        hint = retry_after_s(exc)
        if hint:
            self.hold_until = max(self.hold_until, now + hint)
            self._schedule_wake()
        if slot.generation == self.generation:   # the first 429 of this generation: cut once
            self.generation += 1
            before, self.limit = self.limit, max(self.floor, self.limit // 2)
            self.low_water = min(self.low_water, self.limit)
            log.warning("gemini rate limited (%s): concurrency %d -> %d%s", retrying.describe(exc), before, self.limit,
                        f", holding {hint:.1f}s" if hint else "")
            self._debug(now)

    def _release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self._dispatch()

    # ---- the queue ----

    def _dispatch(self) -> None:
        if self._clock() < self.hold_until:
            self._schedule_wake()
            return
        for q, priority in ((self._interactive, "interactive"), (self._background, "background")):
            while q and self.in_flight < self._cap(priority):
                fut = q.popleft()
                if fut.done():   # cancelled while waiting
                    continue
                self.in_flight += 1
                fut.set_result(Slot(self, self.generation))
        self._maybe_debug()

    def _schedule_wake(self) -> None:
        """Re-run the dispatch when a hold hint passes (there is nothing else to wake the queue then)."""
        delay = self.hold_until - self._clock()
        if delay <= 0 or (self._wake is not None and not self._wake.done()) or not self.queued():
            return

        async def wake():
            await self._sleep(delay)
            self._wake = None
            self._dispatch()
        self._wake = asyncio.ensure_future(wake())

    # ---- the debug line ----

    def _maybe_debug(self) -> None:
        now = self._clock()
        if now - self._last_debug >= DEBUG_EVERY_S and (self.queued() or self.recent_429()):
            self._debug(now)

    def _debug(self, now: float) -> None:
        self._last_debug = now
        log.info("gemini pacer: limit %d, in flight %d, queued %d (%d interactive), 429s in the last minute %d",
                 self.limit, self.in_flight, self.queued(), len(self._interactive), self.recent_429())


shared = Pacer()
_by_model: dict[str, Pacer] = {}


def for_model(model: str | None) -> Pacer:
    """The limiter for one Gemini model. Each model has its own Vertex capacity, so a Flash-Lite 429 storm from book
    processing must not throttle Flash answers. The main model (GEMINI_MODEL) uses `shared`, as every other call site does."""
    if not model or model == settings.gemini_model:
        return shared
    p = _by_model.get(model)
    if p is None:
        p = _by_model[model] = Pacer()
    return p
