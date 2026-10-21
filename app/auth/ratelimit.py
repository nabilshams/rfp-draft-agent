"""Per-user rate limiting for the expensive endpoints.

In-process sliding window. The app runs as a single replica (logs and metrics are in-process too),
so this is enough; move the counters to shared storage before scaling out.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, status

from app.auth.entra import UserClaims, get_current_user
from app.config import Settings, get_settings

_WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int) -> None:
        """Record a request for *key*; raise 429 when it exceeds *limit* per minute (0 = unlimited)."""
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= _WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, math.ceil(_WINDOW_SECONDS - (now - hits[0])))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests; please slow down",
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limited(
    user: UserClaims = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UserClaims:
    """Route dependency: authenticate, then apply the per-user request limit."""
    limiter.check(user.oid, settings.rate_limit_per_minute)
    return user
