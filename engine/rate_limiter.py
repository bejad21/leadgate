import time


class FixedWindowRateLimiter:
    """A simple in-memory, per-key sliding-window rate limiter.

    Tracks the recent hit timestamps for each key (for example a Telegram chat_id) and rejects once
    the limit is exceeded within the window. It is not shared across worker processes or persisted
    across restarts, so several workers would need a shared store such as Redis.

    Old entries are pruned on every call, and idle keys are swept now and then, so memory stays bounded.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[int, list[float]] = {}
        self._calls_since_sweep = 0

    def allow(self, key: int) -> bool:
        """Return False once `key` already has max_requests hits in the window; otherwise record this hit and return True."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        timestamps = self._hits.setdefault(key, [])
        fresh = [t for t in timestamps if t > cutoff]

        if len(fresh) >= self.max_requests:
            self._hits[key] = fresh
            self._periodic_sweep(cutoff)
            return False

        fresh.append(now)
        self._hits[key] = fresh
        self._periodic_sweep(cutoff)
        return True

    def _periodic_sweep(self, cutoff: float) -> None:
        """Occasionally drop keys with no timestamps left in the window, so idle chat_ids don't linger."""
        self._calls_since_sweep += 1
        if self._calls_since_sweep < 200:
            return
        self._calls_since_sweep = 0
        empty_keys = [k for k, ts in self._hits.items() if not any(t > cutoff for t in ts)]
        for k in empty_keys:
            del self._hits[k]
