"""Group-ordered aiogram dispatch â€” PTB semantics for the Pi bot.

PTB ran ALL handler groups per update (one handler per group, groups in
first-registration order).  aiogram stops at the first matching handler.
This module bridges the two:

* ``on(event, fn, group=..., flt=...)`` queues a registration exactly like
  ``Application.add_handler`` did (same ordering rules).
* ``install(dp)`` registers everything on a Dispatcher, sorted by
  **numeric group** (PTB stores handlers in ``{group: [...]}`` and walks
  ``sorted(...)``) and then registration order inside the group.
* Every callback is wrapped so that it raises ``SkipHandler`` after a
  successful run: the observer then continues with the next matching
  handler â€” reproducing "all groups run".  Handler exceptions are
  logged/persisted and the chain continues (PTB ``process_error`` parity).
* A handler that must stop *every* later handler for this update raises
  :class:`StopChain`; ``_wrap`` then returns normally, which is aiogram's
  own "first match wins" signal.  The force-join gate uses it so a
  blocked message can never reach a command handler, an XP tracker or a
  message counter.
 * A data filter injects ``bot_data`` (process-global) and ``chat_data``
  (per-chat) into the handler kwargs of handlers that declare them.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Filter

from bot.command_handler import COMMAND, CommandFilter, MultiPrefixCommand, cmd, parse_command

__all__ = [
    "on", "install", "clear", "snapshot", "entries", "StopChain",
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


class StopChain(Exception):
    """Raise from a handler to stop *every* later handler for this update.

    aiogram's observer stops at the first handler that returns normally
    and only continues when the handler raises ``SkipHandler``.  ``_wrap``
    converts a normal run into ``SkipHandler`` (PTB "all groups run"), so
    a plain ``return`` from a handler can never stop the chain - but a
    handler that must make the update vanish completely raises this
    instead.  ``_wrap`` then returns normally, which is aiogram's own stop
    signal.

    The force-join gate is the motivating case: a message from someone who
    has not joined the bound channel must be deleted *and* must never
    reach a command handler (which would answer it), a counter (which
    would count it) or an XP tracker.
    """


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
_SEQ = 0


def on(event: str, fn: Callable[..., Any], *, group: int = 0, flt: Any = None) -> Callable:
    """Queue ``fn`` for aiogram observer ``event`` (PTB add_handler parity)."""
    global _SEQ
    if event not in (
        "message", "callback_query", "chat_member",
        "my_chat_member", "chat_join_request",
    ):
        raise ValueError(f"unsupported pipeline event: {event!r}")
    _QUEUE.append(Entry(event=event, fn=fn, group=group, flt=flt,
                        seq=_SEQ, module=fn.__module__))
    _SEQ += 1
    return fn


def clear() -> None:
    """Drop the queue (tests / load_modules)."""
    global _SEQ
    _QUEUE.clear()
    _SEQ = 0


def snapshot() -> List[Entry]:
    """Dispatch order: PTB walks ``sorted(handlers)`` — numeric group,
    then registration order inside the group.

    This must be the *number*, not first-registration order: every module
    in this codebase documents its position by number ("filters=1,
    blocklist=2, watchwords=3 -> bind=4", "Group 4 (leveling is 5)"), and
    those comments are only true when the sort is numeric.  It also lets a
    pipeline place itself *before* group 0 with a negative number, which
    is how the force-join gate runs ahead of every command.
    """
    return sorted(_QUEUE, key=lambda e: (e.group, e.seq))


def entries() -> List[Entry]:
    return list(_QUEUE)


def _ordered() -> List[Entry]:
    return snapshot()


def _wrap(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def _inner(*args: Any, **kwargs: Any):
        try:
            await fn(*args, **kwargs)
        except SkipHandler:
            raise
        except StopChain:
            # A normal return is aiogram's own "stop, first match wins".
            # Returning here makes observer.trigger() hand back None
            # instead of UNHANDLED, so no later group ever sees this
            # update.  Must be caught before the blanket Exception below,
            # or StopChain would be reported as a handler error.
            return None
        except Exception as err:  # noqa: BLE001 — PTB process_error parity
            from bot.errors import report_handler_error
            report_handler_error(args[0] if args else None, err)
            raise SkipHandler from None
        # Success also skips: aiogram must keep walking the chain so the
        # NEXT matching group runs (PTB "all groups run for one update").
        # Returning normally here would stop the observer after the first
        # match and silently starve every later group (counting, trackers).
        raise SkipHandler
    return _inner


def _needs_data_filter(fn: Callable) -> bool:
    """True when ``fn`` actually accepts ``chat_data`` / ``bot_data``.

    The data filter was attached to EVERY handler, so each update paid a
    ``chat_data_for()`` getattr-chain + ``dict.setdefault`` (and an extra
    awaited filter) for ~155 handlers, almost none of which use the
    injected kwargs. Handlers that declare them — or swallow extras with
    ``**kwargs`` — still get them; everyone else skips the work.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / no signature
        return True
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name in ("chat_data", "bot_data", "user_data"):
            return True
    return False


def install(dp: Any) -> int:
    """Register every queued handler on ``dp`` in PTB dispatch order."""
    for e in _ordered():
        data_filter = _DataFilter() if _needs_data_filter(e.fn) else None
        flts = [f for f in (e.flt, data_filter) if f is not None]
        getattr(dp, e.event).register(_wrap(e.fn), *flts)
    return len(_QUEUE)
