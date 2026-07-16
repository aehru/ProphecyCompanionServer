"""Tiny in-memory sliding-window rate limiter.

Single-process only — exactly what this server is (one uvicorn worker, SQLite).
A multi-worker or multi-instance deploy needs a shared store instead (redis or
similar); see TODO.md. Also note the key is the *socket peer* IP: behind a
reverse proxy that's the proxy, not the client, until forwarded headers are
handled (TODO.md again).
"""

import time
from collections import defaultdict


class SlidingWindowLimiter:
    """At most `limit` hits per `window_seconds`, per key."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window
        # Drop expired hits from the front (timestamps are appended in order).
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
