"""Cancellation primitives — cancel-aware sleeps for the send loop.

The send loop must be able to stop *between* batches AND *during*
FloodWait pauses. CancelToken.sleep() races the wait against the cancel
event, so a FloodWait never traps a session that the user aborted.
"""

from __future__ import annotations

import asyncio

from .exceptions import SessionCancelled


class CancelToken:
    """One-way cancel flag with cancel-aware waiting."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise SessionCancelled if cancelled (call between steps)."""
        if self._event.is_set():
            raise SessionCancelled()

    async def sleep(self, seconds: float) -> None:
        """Sleep, waking immediately (with SessionCancelled) on cancel."""
        if seconds <= 0:
            self.check()
            return
        done, _ = await asyncio.wait(
            [asyncio.ensure_future(self._event.wait())],
            timeout=seconds,
        )
        if done:  # event was set during the wait
            raise SessionCancelled()
        # Timed out without cancellation — continue normally.
