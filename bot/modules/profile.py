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

from telegram import Update, User
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from bot.command_handler import CommandHandler
from bot.database import db
from bot.emojis import E
from bot.responses import action_card, field_extra, field_user, rank_value, user_label

logger = logging.getLogger(__name__)


async def _resolve_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[User]:
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user
    if context.args:
        arg = context.args[0]
        chat_id = update.effective_chat.id
        try:
            if arg.startswith("@"):
                member = await context.bot.get_chat_member(chat_id, arg)
                return member.user
            member = await context.bot.get_chat_member(chat_id, int(arg))
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
                row = db.get_user(uid)
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
    return update.effective_user


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profile — reputation + activity card for a user."""
    target = await _resolve_target(update, context)
    if not target:
        await update.message.reply_text(
            f"{E.ERROR} Could not find that user.\n"
            "Usage: /profile [@user] or reply with /profile",
            parse_mode=ParseMode.HTML,
        )
        return

    uid = target.id
    # Ensure row exists for reputation math.
    db._ensure_reputation(uid)
    rep = db.get_reputation(uid)
    active_days = db.get_active_days(uid)

    # Chat context only in groups — ranks are derived from daily_messages.
    chat = update.effective_chat
    chat_id = None
    if chat is not None and getattr(chat, "type", None) not in (None, "private"):
        chat_id = chat.id
    info = db.get_user_rank_info(uid, chat_id)

    # Prefer display name; fall back to DB if Telegram user is sparse.
    display_user = target
    if not getattr(target, "first_name", None) and not getattr(target, "username", None):
        row = db.get_user(uid)
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
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _fmt(n: int) -> str:
    return f"{n:,}"


def setup(app: Application) -> list:
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("rep", profile_command))
    return ["/profile", "/rep"]
