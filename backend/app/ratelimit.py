import time


class TokenBucket:
    """Per-key in-memory token bucket. Good enough to stop a public deploy minting unlimited Boson keys."""

    def __init__(self, capacity: int, per_seconds: float, clock=time.monotonic):
        self.capacity, self.rate, self.clock = capacity, capacity / per_seconds, clock
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, last = self._state.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1, now)
        return True
