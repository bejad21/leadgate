import logging
import threading
import time
from typing import Callable

from engine.store import Store

logger = logging.getLogger(__name__)


class FixedWindowRateLimiter:
    """A simple in-memory, per-key sliding-window rate limiter.

    Tracks the recent hit timestamps for each key (for example a Telegram chat_id) and rejects once
    the limit is exceeded within the window. It is not shared across worker processes or persisted
    across restarts, so several workers would need a shared store such as Redis.

    One lock covers every read and write of the hits, because handlers call this from worker threads.

    Old entries are pruned on every call, and idle keys are swept now and then, so memory stays bounded.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[int, list[float]] = {}
        self._calls_since_sweep = 0
        self._lock = threading.Lock()

    def allow(self, key: int) -> bool:
        """Return False once `key` already has max_requests hits in the window; otherwise record this hit and return True."""
        with self._lock:
            return self._allow(key)

    def _allow(self, key: int) -> bool:
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


class StoreRateLimiter:
    """The same limit, counted in the durable store so it survives a restart and is shared by workers.

    `get_store` is called on every hit (the store may be swapped, and is built lazily). If the store
    cannot be reached the hit is counted in a local FixedWindowRateLimiter instead: limits stay in
    force, only no longer shared or durable, until the store answers again.
    """

    def __init__(self, name: str, max_requests: int, window_seconds: int, get_store: Callable[[], Store]):
        self.name = name
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._get_store = get_store
        self._local = FixedWindowRateLimiter(max_requests, window_seconds)
        self._degraded = False

    def allow(self, key: int) -> bool:
        try:
            allowed = self._get_store().hit_rate_limit(f"{self.name}:{key}", self.window_seconds, self.max_requests)
        except Exception as exc:
            if not self._degraded:
                logger.warning("rate limit %s: store unavailable (%s); counting in memory until it answers", self.name, type(exc).__name__)
                self._degraded = True
            return self._local.allow(key)
        if self._degraded:
            logger.info("rate limit %s: store answers again", self.name)
            self._degraded = False
        return allowed
