"""A small fixed-window rate limiter for the unauthenticated auth endpoints.

Scope, stated plainly: this counts in the process's own memory. It blunts
credential stuffing and reset-email flooding against a single API process and
resets on restart. A multi-process or multi-replica deployment needs a shared
counter (Redis) to be exact — the per-account lockout in ``User.locked_until``
is the durable, cross-process half of the defence, and it lives in the database
precisely because this does not.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

# Above this many tracked keys, expired windows are swept. Bounds the memory a
# spray across many source addresses can pin down.
_SWEEP_THRESHOLD = 10_000


class RateLimiter:
    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(
        self, key: str, *, limit: int, window_seconds: int, now: Optional[float] = None
    ) -> bool:
        """Record an attempt and report whether it is within the window budget."""
        moment = now if now is not None else time.monotonic()
        cutoff = moment - window_seconds
        with self._lock:
            if len(self._hits) > _SWEEP_THRESHOLD:
                self._sweep(cutoff)
            window = self._hits.setdefault(key, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(moment)
            return True

    def _sweep(self, cutoff: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# One limiter for every auth endpoint; the key carries the endpoint name so the
# budgets do not bleed into each other.
auth_rate_limiter = RateLimiter()
