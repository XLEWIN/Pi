"""Group-ordered aiogram dispatch â€” PTB semantics for the Pi bot.

PTB ran ALL handler groups per update (one handler per group, groups in
first-registration order).  aiogram stops at the first matching handler.
This module bridges the two:

* ``on(event, fn, group=..., flt=...)`` queues a registration exactly like
  ``Application.add_handler`` did (same ordering rules).
* ``install(dp)`` registers everything on a Dispatcher, sorted by
  (first-seen group rank, registration order).
* Every callback is wrapped so that it raises ``SkipHandler`` after a
  successful run: the observer then continues with the next matching
  handler â€” reproducing "all groups run".  Handler exceptions are
  logged/persisted and the chain continues (PTB ``process_error`` parity).
* An always-on data filter injects ``bot_data`` (process-global) and
  ``chat_data`` (per-chat) into handler kwargs.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Filter

from bot.command_handler import COMMAND, CommandFilter, MultiPrefixCommand, cmd, parse_command

__all__ = [
    "on", "install", "clear", "snapshot", "entries",
    "cmd", "COMMAND", "CommandFilter", "MultiPrefixCommand", "parse_command",
    "GROUPS", "PRIVATE", "SERVICE", "BOT_DATA", "chat_data_for",
]

# â”€â”€ Common filters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#: groups + supergroups (PTB filters.ChatType.GROUPS)
GROUPS = F.chat.type.in_(("group", "supergroup"))
#: private chats (PTB filters.ChatType.PRIVATE)
PRIVATE = F.chat.type == "private"

#: PTB filters.StatusUpdate.ALL â€” any Telegram service/status message.
_SERVICE_FIELDS = (
    "chat_background_set", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "chat_shared", "connected_website",
    "delete_chat_photo", "forum_topic_closed", "forum_topic_created",
    "forum_topic_edited", "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unshown", "giveaway_completed", "giveaway_created",
    "left_chat_member", "message_auto_delete_timer_changed",
    "migrate_to_chat_id", "migrate_from_chat_id", "new_chat_members",
    "new_chat_photo", "new_chat_title", "pinned_message",
    "proximity_alert_triggered", "refunded_payment", "users_shared",
    "video_chat_started", "video_chat_ended", "video_chat_participants_invited",
    "write_access_allowed", "story", "passport_data",
)
SERVICE = F.func(
    lambda m: any(getattr(m, f, None) for f in _SERVICE_FIELDS)
)

# â”€â”€ chat_data / bot_data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#: Process-global bot_data (PTB application.bot_data).
BOT_DATA: Dict[str, Any] = {}
_CHAT_DATA: Dict[Any, Dict[str, Any]] = {}


def chat_data_for(event: Any) -> Dict[str, Any]:
    chat = getattr(event, "chat", None)
    if chat is None:
        msg = getattr(event, "message", None)
        chat = getattr(msg, "chat", None) if msg is not None else None
    key = getattr(chat, "id", None)
    return _CHAT_DATA.setdefault(key, {})


class _DataFilter(Filter):
    """Injects bot_data + chat_data into every handler call."""

    async def __call__(self, event: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"bot_data": BOT_DATA, "chat_data": chat_data_for(event)}


# â”€â”€ Registration queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@dataclass
class Entry:
    event: str
    fn: Callable[..., Any]
    group: int
    flt: Any
    seq: int
    module: str = ""

    @property
    def key(self) -> str:
        return f"{self.fn.__module__}.{self.fn.__qualname__}"


_QUEUE: List[Entry] = []
_GROUP_RANK: Dict[int, int] = {}
_SEQ = 0


def on(event: str, fn: Callable[..., Any], *, group: int = 0, flt: Any = None) -> Callable:
    """Queue ``fn`` for aiogram observer ``event`` (PTB add_handler parity)."""
    global _SEQ
    if event not in (
        "message", "callback_query", "chat_member",
        "my_chat_member", "chat_join_request",
    ):
        raise ValueError(f"unsupported pipeline event: {event!r}")
    if group not in _GROUP_RANK:
        _GROUP_RANK[group] = len(_GROUP_RANK)
    _QUEUE.append(Entry(event=event, fn=fn, group=group, flt=flt,
                        seq=_SEQ, module=fn.__module__))
    _SEQ += 1
    return fn


def clear() -> None:
    """Drop the queue (tests / load_modules)."""
    global _SEQ
    _QUEUE.clear()
    _GROUP_RANK.clear()
    _SEQ = 0


def snapshot() -> List[Entry]:
    """Dispatch order: PTB first-seen group rank, then registration order."""
    return sorted(_QUEUE, key=lambda e: (_GROUP_RANK[e.group], e.seq))


def entries() -> List[Entry]:
    return list(_QUEUE)


def _ordered() -> List[Entry]:
    return snapshot()


def _wrap(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def _inner(*args: Any, **kwargs: Any):
        try:
            return await fn(*args, **kwargs)
        except SkipHandler:
            raise
        except Exception as err:  # noqa: BLE001 â€” PTB process_error parity
            from bot.errors import report_handler_error
            report_handler_error(args[0] if args else None, err)
            raise SkipHandler from None
    return _inner


def install(dp: Any) -> int:
    """Register every queued handler on ``dp`` in PTB dispatch order."""
    for e in _ordered():
        flts = [f for f in (e.flt, _DataFilter()) if f is not None]
        getattr(dp, e.event).register(_wrap(e.fn), *flts)
    return len(_QUEUE)
