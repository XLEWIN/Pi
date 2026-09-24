"""Session manager — exactly one tagging session per chat.

Check-then-create contains NO awaits, so two concurrent /all commands
cannot both win (the event loop is single-threaded).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from telegram import Message

from .cancellation import CancelToken
from .exceptions import AlreadyRunningError
from .metrics import SessionMetrics
from .models import TagSettings


def _make_done() -> asyncio.Event:
    return asyncio.Event()


@dataclass
class TagSession:
    """One /all run in one chat."""

    chat_id: int
    session_id: int                     # tag_sessions.id (0 until created)
    invoker_id: int
    source_message_id: int
    source_chat_id: int
    thread_id: Optional[int]
    status_message: Message             # live progress card (edited)
    settings: TagSettings
    admin_ids: Set[int]
    token: CancelToken = field(default_factory=CancelToken)
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    total: int = 0
    tagged: int = 0
    state: str = "running"              # running|completed|aborted|failed
    finished_at: Optional[float] = None
    task: Optional[object] = None       # asyncio.Task
    done: asyncio.Event = field(default_factory=_make_done)

    @property
    def running(self) -> bool:
        return self.state == "running"


_sessions: Dict[int, TagSession] = {}


def get(chat_id: int) -> Optional[TagSession]:
    return _sessions.get(chat_id)


def is_running(chat_id: int) -> bool:
    s = _sessions.get(chat_id)
    return s is not None and s.running


def create(
    *,
    chat_id: int,
    session_id: int,
    invoker_id: int,
    source_message: Message,
    status_message: Message,
    settings: TagSettings,
    admin_ids: Set[int],
) -> TagSession:
    """Register a session — raises AlreadyRunningError if one exists.

    Synchronous check-then-insert: no awaits between the two, so it is
    atomic under the asyncio loop.
    """
    existing = _sessions.get(chat_id)
    if existing is not None and existing.running:
        raise AlreadyRunningError()
    session = TagSession(
        chat_id=chat_id,
        session_id=session_id,
        invoker_id=invoker_id,
        source_message_id=source_message.message_id,
        source_chat_id=source_message.chat.id,
        thread_id=getattr(source_message, "message_thread_id", None),
        status_message=status_message,
        settings=settings,
        admin_ids=set(admin_ids),
    )
    _sessions[chat_id] = session
    return session


def discard(session: TagSession) -> None:
    """Remove only if this exact session still owns the slot."""
    if _sessions.get(session.chat_id) is session:
        del _sessions[session.chat_id]


def running_sessions() -> list[TagSession]:
    return [s for s in _sessions.values() if s.running]


def active_count() -> int:
    return len(running_sessions())


def finish(session: TagSession, state: str) -> None:
    """Mark terminal state + timestamp (does NOT remove from map —
    the sender discards after editing the final card)."""
    session.state = state
    session.finished_at = time.time()
    session.done.set()


def reset() -> None:
    """Test helper — drop all sessions."""
    _sessions.clear()
