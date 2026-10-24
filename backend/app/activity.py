"""Who is using the deploy: one log line when someone becomes active (first request, or after IDLE_S quiet) and one
per kind of action (non-GET route) at most every ACTION_EVERY_S, so Cloud Logging shows sign-ins and usage without a
line per request. The live site logs the signed-in Google email (IAP-verified); the public API and anonymous visitors
log their client IP. Never logs tokens, headers or request bodies."""
import logging
import threading
import time

log = logging.getLogger("talkback.activity")

IDLE_S = 30 * 60
ACTION_EVERY_S = 60.0
_MAX = 5000

_lock = threading.Lock()
_seen: dict[str, float] = {}               # who -> last request time
_actions: dict[tuple[str, str], float] = {}  # (who, "METHOD /route") -> last logged


def note(who: str, method: str, route: str, now: float | None = None) -> None:
    """Record one request by `who` ("user@example.com" or "ip:1.2.3.4") to `route` (the route template). Never raises."""
    try:
        now = time.monotonic() if now is None else now
        with _lock:
            if len(_seen) > _MAX:
                _seen.clear()
            if len(_actions) > _MAX:
                _actions.clear()
            last = _seen.get(who)
            _seen[who] = now
            fresh = last is None or now - last > IDLE_S
            key = (who, f"{method} {route}")
            act = method not in ("GET", "HEAD", "OPTIONS") and now - _actions.get(key, -1e9) >= ACTION_EVERY_S
            if act:
                _actions[key] = now
        if fresh:
            log.warning("user active: %s", who)
        if act:
            log.warning("user action: %s %s %s", who, method, route)
    except Exception:  # logging must never break a request
        pass


def route_of(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path
