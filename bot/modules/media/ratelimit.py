"""Per-user request rate limiting + per-user active-job caps.

In-memory, sliding window — good enough for one process (the bot runs
on a single Railway instance). Global concurrency lives in
singleflight.GlobalSemaphore instead.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict

from .config import ig_config

_WINDOW_SEC = 60.0


class RateLimiter:
    """Sliding-window requests/min + active-job counter per user."""

    def __init__(self, per_minute: int, jobs_per_user: int) -> None:
        self.per_minute = max(1, per_minute)
        self.jobs_per_user = max(1, jobs_per_user)
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)
        self._active: Dict[int, int] = defaultdict(int)
        self._lock = Lock()

    def check_request(self, user_id: int) -> bool:
        """True if this user may start another request right now."""
        if not user_id:
            return True
        now = time.time()
        with self._lock:
            dq = self._hits[user_id]
            while dq and now - dq[0] > _WINDOW_SEC:
                dq.popleft()
            if len(dq) >= self.per_minute:
                return False
            dq.append(now)
            return True

    def try_acquire_job(self, user_id: int) -> bool:
        if not user_id:
            return True
        with self._lock:
            if self._active[user_id] >= self.jobs_per_user:
                return False
            self._active[user_id] += 1
            return True

    def release_job(self, user_id: int) -> None:
        if not user_id:
            return
        with self._lock:
            left = self._active[user_id] - 1
            if left <= 0:
                self._active.pop(user_id, None)
            else:
                self._active[user_id] = left

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "tracked_users": len(self._hits),
                "active_jobs": sum(self._active.values()),
            }


limiter = RateLimiter(
    per_minute=ig_config.max_requests_per_minute,
    jobs_per_user=ig_config.max_active_jobs_per_user,
)
