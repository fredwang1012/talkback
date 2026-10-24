import time


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
