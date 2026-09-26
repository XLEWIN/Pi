"""Single-flight: only one resolve/download job per media key at a time."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, TypeVar

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


async def run_exclusive(key: str, factory: Callable[[], Awaitable[T]]) -> T:
    """
    Run *factory* under a per-key lock.

    If the key is already in-flight, raise IGBusy immediately (no queue pile-up).
    """
    lock = await _get_lock(key)
    if lock.locked():
        metrics.bump("busy_rejects")
        raise IGBusy()
    async with lock:
        try:
            return await factory()
        finally:
            if not lock.locked() and len(_locks) > 64:
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
