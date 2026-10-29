"""Bounded retries with exponential backoff and jitter for the transient failures of Google calls (Gemini, embeddings,
Vector Search over httpx / google-genai).

Transient: network errors (httpx ReadError / ConnectError / RemoteProtocolError / timeouts, reset connections), rate
limits (429) and server errors (5xx). Never retried: quota exhaustion (insufficient_quota, billing), auth (401/403) and
every other client error. Logs name only the exception type and HTTP status (a
message may quote book text or a host). Gemini calls add the shared adaptive limit of app/pacer.py on top."""
import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger("talkback.retry")
T = TypeVar("T")

ATTEMPTS = 4      # calls in total (so at most 3 retries)
BASE_S = 0.5
CAP_S = 8.0
NEVER = ("insufficient_quota", "billing", "credit balance")   # not "quota exceeded": Vertex says that for per-minute limits
AUTH_NAMES = ("DefaultCredentialsError", "RefreshError", "PermissionDenied", "Unauthenticated")
# google.api_core / httpcore / stdlib names that mean "try again" even when no status code is attached
TRANSIENT_NAMES = frozenset({
    "ServiceUnavailable", "DeadlineExceeded", "InternalServerError", "TooManyRequests", "ResourceExhausted", "Aborted",
    "GatewayTimeout", "BadGateway", "RemoteDisconnected", "IncompleteRead", "ServerError",
    "ReadError", "WriteError", "ConnectError", "RemoteProtocolError", "ReadTimeout", "WriteTimeout", "ConnectTimeout",
    "PoolTimeout", "LocalProtocolError",
})


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def status_of(exc: BaseException) -> int | None:
    """The HTTP status an API error carries (google-genai `.code`, SDKs' `.status_code`), else None."""
    for attr in ("code", "status_code"):
        code = getattr(exc, attr, None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def describe(exc: BaseException) -> str:
    """"ClientError 429": the exception type and the HTTP status it (or its cause) carries; never the message."""
    code = next((c for e in _chain(exc) if (c := status_of(e)) is not None), None)
    return f"{type(exc).__name__} {code}" if code is not None else type(exc).__name__


def is_quota_or_auth(exc: BaseException) -> bool:
    """Out of credit / quota or not authorised: retrying can only waste time (and the caps must see it at once)."""
    chain = list(_chain(exc))
    ai_studio_429 = any(status_of(e) == 429 for e in chain)
    for e in chain:
        if status_of(e) in (401, 402, 403) or type(e).__name__ in AUTH_NAMES:
            return True
        if any(m in str(e).lower() for m in NEVER) and not (ai_studio_429 and _ai_studio_per_minute_text(e)):
            return True
    return False


def _ai_studio_per_minute_text(exc: BaseException) -> bool:
    """Google AI Studio (GEMINI_API_KEY) words every 429 "check your plan and billing details", per-minute limits
    included: those are waited out like Vertex's. Only a per-day quota (QuotaFailure quotaId "...PerDay...") is not.
    Judged on the text alone, so a wrapper repeating the 429's message (no status of its own) reads the same; the
    caller requires a 429 somewhere in the chain."""
    msg = str(exc).lower()
    return "plan and billing details" in msg and "perday" not in msg


def is_daily_quota(exc: BaseException) -> bool:
    """An AI Studio per-day quota 429: final until the quota resets, so it must not throttle the shared pacer either."""
    return any(status_of(e) == 429 and "plan and billing details" in (m := str(e).lower()) and "perday" in m
               for e in _chain(exc))


def is_transient(exc: BaseException) -> bool:
    """True for a failure that a later identical call may well survive (network, 429, 5xx); see the module doc."""
    if is_quota_or_auth(exc):
        return False
    for e in _chain(exc):
        if isinstance(e, (httpx.TransportError, asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        code = status_of(e)
        if code == 429 or (code is not None and 500 <= code < 600):
            return True
        if type(e).__name__ in TRANSIENT_NAMES:
            return True
    return False


def backoff(attempt: int, base: float = BASE_S, cap: float = CAP_S) -> float:
    """Seconds to wait after the (0-based) failed `attempt`: exponential, capped, with "equal jitter" (half fixed, half
    random) so parallel jobs that failed together do not retry together."""
    full = min(cap, base * 2 ** attempt)
    return full / 2 + random.uniform(0, full / 2)


async def retry_async(fn: Callable[[], Awaitable[T]], *, what: str, attempts: int = ATTEMPTS, base: float = BASE_S,
                      cap: float = CAP_S, sleep: Callable[[float], Awaitable[None]] | None = None,
                      retry_if: Callable[[BaseException], bool] = is_transient) -> T:
    """`await fn()` up to `attempts` times while it fails with a `retry_if` error; the last error is raised as is."""
    sleep = sleep or asyncio.sleep
    for attempt in range(attempts):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt >= attempts - 1 or not retry_if(e):
                raise
            delay = backoff(attempt, base, cap)
            log.warning("%s failed (%s); retry %d/%d in %.1fs", what, describe(e), attempt + 1, attempts - 1, delay)
            await sleep(delay)
    raise AssertionError("unreachable")


def retry_sync(fn: Callable[[], T], *, what: str, attempts: int = ATTEMPTS, base: float = BASE_S, cap: float = CAP_S,
               sleep: Callable[[float], None] = time.sleep, retry_if: Callable[[BaseException], bool] = is_transient) -> T:
    """The blocking twin of retry_async (worker threads: embeddings)."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if attempt >= attempts - 1 or not retry_if(e):
                raise
            delay = backoff(attempt, base, cap)
            log.warning("%s failed (%s); retry %d/%d in %.1fs", what, describe(e), attempt + 1, attempts - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")
