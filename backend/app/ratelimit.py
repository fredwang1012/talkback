import ipaddress
import time

from fastapi import Request

from .config import settings


class TokenBucket:
    """Per-key in-memory token bucket. Good enough to stop a public deploy minting unlimited Boson keys."""

    def __init__(self, capacity: int, per_seconds: float, clock=time.monotonic, max_keys: int = 10_000):
        self.capacity, self.rate, self.clock, self.max_keys = capacity, capacity / per_seconds, clock, max_keys
        self._state: dict[str, tuple[float, float]] = {}  # insertion order = least recently touched first

    def allow(self, key: str) -> bool:
        now = self.clock()
        if key not in self._state and len(self._state) >= self.max_keys:
            del self._state[next(iter(self._state))]  # evict the oldest-touched key
        tokens, last = self._state.pop(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1, now)
        return True


def client_ip(request: Request) -> str:
    """Peer address. With TRUST_PROXY (Cloud Run is the only ingress) use the right-most X-Forwarded-For entry:
    the front end appends the real client there, while anything to its left is client-supplied and spoofable."""
    if settings.trust_proxy:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def normalize_ip(ip: str) -> str:
    """Rate-limit key for an address: IPv4 as is, IPv4-mapped IPv6 as its IPv4, other IPv6 as its /64 network
    (one subscriber commonly holds a whole /64, so rotating inside it must not buy a fresh allowance)."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def client_key(request: Request) -> str:
    """`client_ip`, normalized for per-address limits (used by the public API)."""
    return normalize_ip(client_ip(request))
