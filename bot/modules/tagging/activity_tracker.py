"""Activity write-behind — buffer message touches, flush every ~3s.

Hot path (every group message) touches an in-memory dict; a lazily
started task persists batches of changes to tag_activity + tag_members.
This keeps per-message cost at O(1) memory ops instead of DB writes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from bot.logger import logger

from . import config, database as tdb

# (chat_id, user_id) → buffered entry
_BufferKey = Tuple[int, int]


@dataclass
class _Entry:
    count: int = 0
    last_ts: float = 0.0
    username: Optional[str] = None
    display_name: Optional[str] = None
    is_bot: bool = False


_buffer: Dict[_BufferKey, _Entry] = {}
_task: Optional[asyncio.Task] = None
_stop = False


def touch(chat_id: int, user, now: Optional[float] = None) -> None:
    """Record one message from `user` (a telegram.User) — O(1), no I/O."""
    uid = getattr(user, "id", None)
    if uid is None:
        return
    ts = now if now is not None else time.time()
    key = (chat_id, uid)
    entry = _buffer.get(key)
    if entry is None:
        entry = _Entry()
        _buffer[key] = entry
    entry.count += 1
    if ts > entry.last_ts:
        entry.last_ts = ts
        username = getattr(user, "username", None)
        if username:
            entry.username = username
        name = " ".join(
            p for p in (
                getattr(user, "first_name", None) or "",
                getattr(user, "last_name", None) or "",
            ) if p
        )
        if name:
            entry.display_name = name
        if getattr(user, "bot", False):
            entry.is_bot = True
    _ensure_task()


def _ensure_task() -> None:
    """Lazy-start the flush loop (never at import time)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop yet — the next touch after startup will start it
    _task = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    while not _stop:
        await asyncio.sleep(config.FLUSH_INTERVAL)
        # Off the loop: a flush is two bulk_write round-trips.
        await flush_async()


def _drain():
    """Atomically move the buffer into write lists. No I/O.

    MUST stay on the event loop: ``touch()`` also runs there, so a drain
    and a touch can never interleave. Returns None when there is nothing
    buffered, otherwise (members, counters).
    """
    if not _buffer:
        return None
    snapshot = dict(_buffer)
    _buffer.clear()
    members = []
    counters = []
    for (chat_id, user_id), entry in snapshot.items():
        if entry.last_ts <= 0:
            continue
        members.append((
            chat_id, user_id, entry.username, entry.display_name,
            int(entry.is_bot), entry.last_ts,
        ))
        counters.append((chat_id, user_id, entry.count, entry.last_ts))
    return members, counters


def _persist(members, counters) -> None:
    """Blocking MongoDB writes — MUST run in a worker thread."""
    try:
        tdb.bulk_observe(members)   # identity + last_active (1 commit)
        tdb.flush_activity(counters)  # counters (1 commit)
    except Exception as e:
        logger.warning(f"Tagging activity flush error: {e}")


def flush_now() -> int:
    """Persist the buffer immediately; returns number of entries flushed."""
    drained = _drain()
    if drained is None:
        return 0
    members, counters = drained
    _persist(members, counters)
    return len(counters)


async def flush_async() -> int:
    """flush_now() with the blocking writes moved off the event loop.

    The drain still happens here (see _drain) — only the two bulk_write
    round-trips run in the worker thread.
    """
    drained = _drain()
    if drained is None:
        return 0
    members, counters = drained
    await asyncio.to_thread(_persist, members, counters)
    return len(counters)


def pending() -> int:
    return len(_buffer)


def stop() -> None:
    """Stop the loop and flush leftovers (used by tests/shutdown)."""
    global _stop, _task
    _stop = True
    if _task is not None:
        _task.cancel()
        _task = None
    flush_now()


def reset() -> None:
    """Test helper: clear buffer + allow a fresh loop."""
    global _stop, _task
    _stop = False
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    _buffer.clear()
