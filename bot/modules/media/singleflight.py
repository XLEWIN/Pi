"""Single-flight: only one resolve/download job per media key at a time.

Concurrent requests for the SAME link now wait for the in-flight job
(spec §16 "attach"): the leader does the extraction/download/upload, the
waiters block on the lock and then run their own job — which hits the
fresh file_id cache and sends instantly.  Only a genuine timeout (job
still running after *timeout*) raises IGBusy.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional, TypeVar

from .exceptions import IGBusy
from .metrics import metrics

T = TypeVar("T")

_locks: Dict[str, asyncio.Lock] = {}


async def _get_lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def run_exclusive(
    key: str,
    factory: Callable[[], Awaitable[T]],
    *,
    wait: bool = True,
    timeout: Optional[float] = None,
) -> T:
    """
    Run *factory* under a per-key lock.

    wait=True (default): block until the lock frees, then run — waiters
    typically become cache hits after the leader finishes.  *timeout*
    bounds the wait; on expiry IGBusy is raised so users get feedback
    instead of an infinite hang.
    wait=False: raise IGBusy immediately when the key is in flight.
    """
    lock = await _get_lock(key)
    if not lock.locked():
        async with lock:
            try:
                return await factory()
            finally:
                if not lock.locked() and len(_locks) > 64:
                    _locks.pop(key, None)
    if not wait:
        metrics.bump("busy_rejects")
        raise IGBusy()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        metrics.bump("busy_rejects")
        raise IGBusy() from None
    try:
        return await factory()
    finally:
        lock.release()
        if len(_locks) > 64:
            _locks.pop(key, None)


class GlobalSemaphore:
    """Env-configured concurrency cap for download jobs."""

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(max(1, limit))

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False
