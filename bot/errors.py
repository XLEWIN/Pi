"""Handler error reporting (PTB on_error / process_error parity).

Both the pipeline wrapper (handler exceptions) and ``dp.errors``
(middleware/unexpected) funnel into :func:`report_handler_error`.
Full tracebacks land in ``handler_errors.log`` next to the DB.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any, Optional, Tuple

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.types.error_event import ErrorEvent

from bot.database import DB_DIR
from bot.logger import logger

HANDLER_ERRORS_FILE = DB_DIR / "handler_errors.log"

#: PTB classified these names as soft network/API noise.
_SOFT_NAMES = {"TimedOut", "BadRequest", "TimeoutException", "ConnectError",
               "ReadError", "WriteError", "RemoteProtocolError"}


def _is_soft(err: BaseException) -> bool:
    # isinstance (not just the type name) so every network subclass —
    # aiogram TelegramNetworkError, httpx wrapping, asyncio timeouts —
    # is treated as network, matching the old NetworkError check.
    if isinstance(err, (TelegramNetworkError, TimeoutError)):
        return True
    if isinstance(err, TelegramBadRequest):
        return True
    if isinstance(err, TelegramAPIError):
        return False
    return type(err).__name__ in _SOFT_NAMES


def _event_ids(event: Any) -> Tuple[Any, Any, Any]:
    """(update_ref, chat, user) for log lines — best effort, never raises."""
    ref = getattr(event, "update_id", None) or getattr(event, "message_id", None) or "?"
    chat_obj = getattr(event, "chat", None)
    if chat_obj is None:
        msg = getattr(event, "message", None)
        chat_obj = getattr(msg, "chat", None)
    chat = getattr(chat_obj, "id", "?")
    user_obj = getattr(event, "from_user", None)
    if user_obj is None:
        user_obj = getattr(event, "user", None)
    user = getattr(user_obj, "id", "?")
    return ref, chat, user


def persist_handler_error(event: Any, err: BaseException) -> None:
    """Append full traceback to a log file (diagnosable without a paste)."""
    try:
        uid, chat, user = _event_ids(event)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        with open(HANDLER_ERRORS_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"=== {ts} update={uid} chat={chat} user={user} "
                f"{type(err).__name__}: {err}\n{tb}\n"
            )
    except Exception:
        pass  # diagnosis must never raise


def report_handler_error(event: Any, err: BaseException,
                         update_ref: Optional[Any] = None) -> None:
    """PTB on_error body: soft errors get one line, the rest persist."""
    if _is_soft(err):
        logger.warning(f"Handler network/API issue: {err}")
        return
    logger.error("Handler error", exc_info=err)
    if event is not None:
        try:
            ref, chat, user = _event_ids(event)
            logger.error("  update=%s chat=%s user=%s",
                         update_ref if update_ref is not None else ref, chat, user)
        except Exception:
            pass
    persist_handler_error(event, err)


async def on_error(event: ErrorEvent, bot=None) -> None:  # noqa: ANN001
    """``dp.errors`` handler — logs handler/middleware exceptions."""
    err = event.exception
    update = event.update
    report_handler_error(update, err)


__all__ = ["on_error", "report_handler_error", "persist_handler_error",
           "HANDLER_ERRORS_FILE"]
