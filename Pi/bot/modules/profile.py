"""Profile — user reputation card.

/profile [@user | reply]
Shows reputation, messages, active-since, rank, positives/warnings/restrictions.
"""

import logging
from datetime import datetime
from html import escape
from typing import Optional

from telegram import Update, User
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.database import db
from bot.emojis import E
from bot.responses import action_card, field_extra, field_user, user_label

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
    # Ensure row exists for rank math.
    db._ensure_reputation(uid)
    rep = db.get_reputation(uid)
    rank = db.get_reputation_rank(uid)
    active_days = db.get_active_days(uid)

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
        field_extra(E.STAR, "REPUTATION", _fmt(score)),
        field_extra(E.INFO, "MESSAGES", _fmt(messages)),
        field_extra(E.TIME, "ACTIVE SINCE", f"{active_days} days"),
        field_extra(E.MEDAL_1, "RANK", f"#{rank}"),
        field_extra(E.CHECK, "POSITIVE ACTIONS", str(pos)),
        field_extra(E.WARN, "WARNINGS", str(warns)),
        field_extra(E.BAN, "RESTRICTIONS", str(restr)),
    ]
    text = action_card("USER PROFILE", fields, icon=E.USER)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _fmt(n: int) -> str:
    return f"{n:,}"


def setup(app: Application) -> list:
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("rep", profile_command))
    return ["/profile", "/rep"]
