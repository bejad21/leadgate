import time


class FixedWindowRateLimiter:
    """A simple in-memory, per-key fixed-window rate limiter.

    Good enough for a single-process portfolio deployment: it tracks, per
    key (e.g. a Telegram chat_id), the timestamps of recent hits within a
    rolling window and rejects once the limit is exceeded. Not shared
    across worker processes and not persisted across restarts - a
    production deployment with multiple workers would need a shared store
    (e.g. Redis) instead.

    Old entries are pruned on every call (both for the key being checked
    and, occasionally, across the whole table) so memory does not grow
    without bound over a long-running process even if many distinct
    chat_ids show up once and never return.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[int, list[float]] = {}
        self._calls_since_sweep = 0

    def allow(self, key: int) -> bool:
        """Record a hit for `key` and return whether it's within the limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        timestamps = self._hits.setdefault(key, [])
        # Drop timestamps that have aged out of the window.
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
        """Occasionally drop keys with no timestamps left in the window, so
        chat_ids that stop sending requests don't linger in memory forever.
        """
        self._calls_since_sweep += 1
        if self._calls_since_sweep < 200:
            return
        self._calls_since_sweep = 0
        empty_keys = [k for k, ts in self._hits.items() if not any(t > cutoff for t in ts)]
        for k in empty_keys:
            del self._hits[k]
