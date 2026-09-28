"""Owner-only /broadcast — boa's broadcast system on PTB.

Reply to any message with:

    /broadcast            → every tracked chat + user
    /broadcast -chat      → groups only
    /broadcast -user      → users only
    /broadcast -pin       → also pin in groups (owner-rights required)

The system mirrors boa: an "In Progress" card with a red Cancel
button, one forward per target with a 1.5 s gap, FloodWait retried
after ``retry_after``, dead targets skipped silently, then the status
message is edited into a Completed/Cancelled card with users+groups
reached.

Cancel is a module flag (one owner, one broadcast at a time — same
assumption boa makes).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Tuple

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import btn_danger, build_keyboard
from bot.pipeline import cmd, on
from bot.reply import reply_text

logger = logging.getLogger(__name__)

_CB = "broadcast"
_SEND_DELAY = 1.5        # seconds between forwards (boa's pacing)
_cancel = False          # set by the Cancel button; checked per target

_TARGET_LABEL = {
    "all": "everyone",
    "chat": "chats only",
    "user": "users only",
}


def _is_owner(user) -> bool:
    return bool(user is not None and settings.owner_id
                and user.id == settings.owner_id)


# ── Cards ────────────────────────────────────────────────────────

def _usage_text() -> str:
    return (
        f"{E.INFO} Reply to a message with /broadcast.\n"
        "   - /broadcast → everyone\n"
        "   - /broadcast -chat → groups only\n"
        "   - /broadcast -user → users only\n"
        "   - /broadcast -pin → pin in groups"
    )


def _progress_text(target: str) -> str:
    return (
        f"{E.FORWARD} Broadcast In Progress\n"
        f"├ {E.INFO} Target: {_TARGET_LABEL[target]}\n"
        f"└ {E.INFO} Sending..."
    )


def _done_text(user_count: int, group_count: int, cancelled: bool) -> str:
    header = (
        f"{E.CROSS} Broadcast Cancelled" if cancelled
        else f"{E.SUCCESS} Broadcast Completed"
    )
    return (
        f"{header}\n"
        f"├ {E.USER} Users Reached: {user_count}\n"
        f"└ {E.ANNOUNCE} Groups Reached: {group_count}"
    )


def _progress_kb():
    return build_keyboard(
        [[btn_danger("Cancel Broadcast", f"{_CB}:cancel", EID.CROSS)]]
    )


# ── The loop (boa's broadcast_message) ──────────────────────────

async def _forward_once(bot, target_id: int, reply):
    """One forward — returns the sent Message (raises on failure)."""
    return await bot.forward_message(
        target_id, reply.chat.id, reply.message_id
    )


async def _run_broadcast(bot, reply, groups: List[int],
                         users: List[int], pin: bool,
                         target: str) -> Tuple[int, int, bool]:
    """Forward ``reply`` to every target; returns (users, groups, cancelled)."""
    user_count, group_count = 0, 0

    if target in ("all", "chat"):
        for chat_id in groups:
            if _cancel:
                break
            try:
                sent = await _forward_once(bot, chat_id, reply)
                if pin and sent is not None:
                    try:
                        await bot.pin_chat_message(
                            chat_id, sent.message_id,
                            disable_notification=False,
                        )
                    except TelegramAPIError as e:
                        logger.debug("broadcast pin failed in %s: %s",
                                     chat_id, e)
                group_count += 1
                await asyncio.sleep(_SEND_DELAY)
            except TelegramRetryAfter as e:
                # FloodWait — wait it out, then retry this chat once.
                await asyncio.sleep(e.retry_after)
                try:
                    await _forward_once(bot, chat_id, reply)
                    group_count += 1
                except TelegramAPIError:
                    pass
            except TelegramAPIError as e:
                logger.debug("broadcast skipped %s: %s", chat_id, e)

    if target in ("all", "user"):
        for user_id in users:
            if _cancel:
                break
            try:
                await _forward_once(bot, user_id, reply)
                user_count += 1
                await asyncio.sleep(_SEND_DELAY)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await _forward_once(bot, user_id, reply)
                    user_count += 1
                except TelegramAPIError:
                    pass
            except TelegramAPIError as e:
                logger.debug("broadcast skipped user %s: %s", user_id, e)

    return user_count, group_count, _cancel


# ── Handler ─────────────────────────────────────────────────────

async def broadcast_command(message: Message,
                            bot: Bot, args: list) -> None:
    """/broadcast — owner-only forward of the replied message."""
    global _cancel

    msg = message
    if msg is None:
        return
    if not _is_owner(message.from_user):
        await reply_text(
            msg,
            f"{E.CROWN} Only the bot owner can broadcast.",
            parse_mode=ParseMode.HTML,
        )
        return

    reply = getattr(msg, "reply_to_message", None)
    if reply is None:
        await reply_text(
            msg,
            _usage_text(), parse_mode=ParseMode.HTML
        )
        return

    args = list(args or [])
    target = "all"
    if "-user" in args:
        target = "user"
    elif "-chat" in args:
        target = "chat"
    pin = "-pin" in args

    _cancel = False
    groups: List[int] = []
    users: List[int] = []
    if target in ("all", "chat"):
        groups = await asyncio.to_thread(db.get_all_chat_ids)
    if target in ("all", "user"):
        users = await asyncio.to_thread(db.get_all_user_ids)
    if not groups and not users:
        await reply_text(
            msg,
            f"{E.WARN} No broadcast targets in the database yet.",
            parse_mode=ParseMode.HTML,
        )
        return

    status = await reply_text(
        msg,
        _progress_text(target),
        parse_mode=ParseMode.HTML,
        reply_markup=_progress_kb(),
    )

    user_count, group_count, cancelled = await _run_broadcast(
        bot, reply, groups, users, pin, target
    )

    try:
        await status.edit_text(
            _done_text(user_count, group_count, cancelled),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("broadcast final edit failed: %s", e)


async def broadcast_callback(callback_query: CallbackQuery) -> None:
    """broadcast:* — Cancel button (owner-only)."""
    global _cancel
    query = callback_query
    if query is None or not str(query.data or "").startswith(f"{_CB}:"):
        return
    if not _is_owner(query.from_user):
        try:
            await query.answer(
                "Only the bot owner can cancel the broadcast.",
                show_alert=True,
            )
        except Exception:
            pass
        return

    action = str(query.data).split(":")[1] if ":" in str(query.data) else ""
    if action != "cancel":
        try:
            await query.answer("Unknown option", show_alert=True)
        except Exception:
            pass
        return

    _cancel = True
    try:
        await query.message.edit_text(
            f"{E.CROSS} Broadcast Cancelled — wrapping up...",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    try:
        await query.answer("Broadcast cancelled.", show_alert=True)
    except Exception:
        pass


def setup() -> List[str]:
    """Register /broadcast + its cancel callback."""
    on("message", broadcast_command, flt=cmd("broadcast", "bcast"))
    on(
        "callback_query", broadcast_callback,
        flt=F.data.regexp(re.compile(rf"^{_CB}:")),
    )
    return ["/broadcast", "/bcast"]
