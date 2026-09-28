"""Profile — user reputation card.

/profile [@user | reply]
Shows reputation, messages, chat/global rank, active-since,
positives/warnings/restrictions. Ranks derive from daily_messages —
the same counts /rankings and /rank display.
"""

import logging
from datetime import datetime
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import Message, User

from bot.database import db
from bot.emojis import E
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.responses import action_card, field_extra, field_user, rank_value, user_label
from bot.async_bridge import adb

logger = logging.getLogger(__name__)


async def _resolve_target(
    message: Message, bot: Bot, args: list
) -> Optional[User]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if args:
        arg = args[0]
        chat_id = message.chat.id
        try:
            if arg.startswith("@"):
                member = await bot.get_chat_member(chat_id, arg)
                return member.user
            member = await bot.get_chat_member(chat_id, int(arg))
            return member.user
        except Exception:
            # Fall through — may be a bare ID the bot can't resolve as member.
            try:
                uid = int(arg)
                # Synthesize a minimal user-like object for DB lookup.
                class _U:
                    id = uid
                    first_name = None
                    last_name = None
                    username = None
                    is_bot = False
                    full_name = None
                u = _U()
                row = await adb(db.get_user(uid))
                if row:
                    u.first_name = row.get("first_name")
                    u.last_name = row.get("last_name")
                    u.username = row.get("username")
                    u.full_name = " ".join(
                        p for p in (row.get("first_name"), row.get("last_name")) if p
                    ) or None
                return u  # type: ignore
            except Exception:
                return None
    return message.from_user


async def profile_command(message: Message, bot: Bot, args: list):
    """/profile — reputation + activity card for a user."""
    target = await _resolve_target(message, bot, args)
    if not target:
        await reply_text(message, 
            f"{E.ERROR} Could not find that user.\n"
            "Usage: /profile [@user] or reply with /profile",
            parse_mode=ParseMode.HTML,
        )
        return

    uid = target.id
    # Ensure row exists for reputation math.
    await adb(db._ensure_reputation(uid))
    rep = await adb(db.get_reputation(uid))
    active_days = await adb(db.get_active_days(uid))

    # Chat context only in groups — ranks are derived from daily_messages.
    chat = message.chat
    chat_id = None
    if chat is not None and getattr(chat, "type", None) not in (None, "private"):
        chat_id = chat.id
    info = await adb(db.get_user_rank_info(uid, chat_id))

    # Prefer display name; fall back to DB if Telegram user is sparse.
    display_user = target
    if not getattr(target, "first_name", None) and not getattr(target, "username", None):
        row = await adb(db.get_user(uid))
        if row:
            class _U:
                id = uid
                first_name = row.get("first_name")
                last_name = row.get("last_name")
                username = row.get("username")
                is_bot = False
                full_name = None
            display_user = _U()  # type: ignore

    messages = int(rep.get("messages") or 0)
    score = int(rep.get("reputation") or 0)
    pos = int(rep.get("positive_actions") or 0)
    warns = int(rep.get("warnings") or 0)
    restr = int(rep.get("restrictions") or 0)

    fields = [
        field_user(display_user),
        field_extra(E.STAR, "Reputation", _fmt(score)),
        field_extra(E.INFO, "Messages", _fmt(messages)),
        field_extra(E.TIME, "Active Since", f"{active_days} days"),
    ]
    if chat_id is not None:
        fields.append(field_extra(
            E.CROWN, "Chat Rank",
            rank_value(info["chat_rank"], info["chat_position"],
                       info["chat_members"], info["chat_messages"]),
        ))
    fields.append(field_extra(
        E.WEB, "Global Rank",
        rank_value(info["global_rank"], info["global_position"],
                   info["global_members"], info["global_messages"]),
    ))
    fields.extend([
        field_extra(E.CHECK, "Positive Actions", str(pos)),
        field_extra(E.WARN, "Warnings", str(warns)),
        field_extra(E.BAN, "Restrictions", str(restr)),
    ])
    text = action_card("User Profile", fields, icon=E.USER)
    await reply_text(message, text, parse_mode=ParseMode.HTML)


def _fmt(n: int) -> str:
    return f"{n:,}"


def setup() -> list:
    on("message", profile_command, flt=cmd("profile"))
    on("message", profile_command, flt=cmd("rep"))
    return ["/profile", "/rep"]
