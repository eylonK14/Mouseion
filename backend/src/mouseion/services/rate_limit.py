"""A modest per-client sliding-window limiter for the ingest entry point."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int, now: float | None = None) -> int:
        """Record an allowed request; return retry-after seconds when denied."""
        current = time.monotonic() if now is None else now
        cutoff = current - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, round(events[0] + window_seconds - current))
            events.append(current)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


ingest_limiter = SlidingWindowLimiter()
