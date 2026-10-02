"""Content locks — ported from the boa reference bot, restyled for Pi.

Commands: /lock <type(s)>, /unlock <type(s)>, /locks, /locktypes.
Enforcement mirrors Pi's own lockdown design: a flag-based gate that
deletes violating messages from non-admins (no per-member mass API
calls), registered before the flood counters so locked messages never
reach XP or burst detection.

All 39 boa lock types are kept, with descriptions surfaced through
colored inline buttons on /locktypes (alert popups on tap).
"""

from __future__ import annotations

import re
from html import escape
from typing import Dict, Set

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import CallbackQuery, Message

from bot.async_bridge import adb
from bot.database import db
from bot.emojis import E
from bot.keyboards.colored import btn_default, build_keyboard
from bot.logger import logger
from bot.modules.security import _is_admin
from bot.pipeline import GROUPS, SERVICE, cmd, on
from bot.reply import reply_text

#: boa parity: every lock type with its /locktypes description.
LOCKABLES: Dict[str, str] = {
    "all": "Restricts everything.",
    "album": "Deletes photos or videos sent as an album.",
    "anonchannel": "Messages sent through anonymous channels.",
    "audio": "Restricts audio media messages.",
    "bot": "Restricts inline bot usage (via_bot).",
    "botlink": "Messages mentioning bots or usernames ending with 'bot'.",
    "btn": "Messages containing inline buttons.",
    "cjk": "Messages containing Chinese, Japanese, or Korean characters.",
    "command": "Messages starting with '/'.",
    "contact": "Restricts contact media messages.",
    "cyrillic": "Messages containing Cyrillic characters.",
    "document": "Restricts document media messages.",
    "email": "Messages containing emails.",
    "emoji": "Messages containing any emoji.",
    "emoji_custom": "Messages containing custom Telegram emojis.",
    "dice": "Messages with dice animations.",
    "external_reply": "Replies to messages from other chats.",
    "forward": "Restricts forwarded messages.",
    "game": "Restricts game messages.",
    "gif": "Restricts GIFs.",
    "inline": "Restricts inline bot results.",
    "invitelink": "Messages containing Telegram invite links (t.me/…).",
    "location": "Restricts location media messages.",
    "phone": "Messages containing phone numbers.",
    "photo": "Restricts photo messages.",
    "poll": "Restricts polls.",
    "rtl": "Messages with right-to-left characters (Arabic, Hebrew…).",
    "spoiler": "Messages containing spoilers.",
    "sticker": "Restricts stickers.",
    "animated_sticker": "Restricts animated stickers.",
    "premium_sticker": "Restricts premium stickers.",
    "text": "Restricts text messages.",
    "url": "Messages containing URLs.",
    "video": "Restricts video messages.",
    "videonote": "Restricts video notes.",
    "voice": "Restricts voice messages.",
}


# ── Enforcement ──────────────────────────────────────────────────
def _violates(locks: Set[str], m: Message) -> bool:
    """True when this message hits any active lock (boa parity)."""
    if "all" in locks:
        return True
    text = m.text or ""
    ents = m.entities or []
    cap_ents = m.caption_entities or []
    types = {e.type for e in ents}
    cap_types = {e.type for e in cap_ents}
    low = text.lower()

    checks = (
        ("album", m.media_group_id is not None),
        ("anonchannel", m.sender_chat is not None
         and not bool(getattr(m.sender_chat, "is_forum", False))),
        ("audio", m.audio is not None),
        ("bot", m.via_bot is not None),
        ("botlink", "mention" in types and "bot" in low),
        ("btn", m.reply_markup is not None),
        ("cjk", bool(text) and any(0x4E00 <= ord(c) <= 0x9FFF for c in text)),
        ("command", text.startswith("/")),
        ("contact", m.contact is not None),
        ("cyrillic", bool(text) and any(0x0400 <= ord(c) <= 0x04FF
                                        for c in text)),
        ("document", m.document is not None),
        ("email", "email" in types),
        ("emoji", bool(text) and any(ord(c) >= 0x1F000 for c in text)),
        ("emoji_custom", "custom_emoji" in types),
        ("dice", m.dice is not None),
        ("external_reply", getattr(m, "external_reply", None) is not None),
        ("forward", getattr(m, "forward_origin", None) is not None),
        ("game", m.game is not None),
        ("gif", m.animation is not None),
        ("inline", m.via_bot is not None),
        ("invitelink", "t.me/" in low or "telegram.me/" in low),
        ("location", m.location is not None),
        ("phone", "phone_number" in types),
        ("photo", m.photo is not None),
        ("poll", m.poll is not None),
        ("rtl", bool(text) and any(0x0590 <= ord(c) <= 0x08FF for c in text)),
        ("spoiler", "spoiler" in types),
        ("sticker", m.sticker is not None),
        ("animated_sticker", m.sticker is not None
         and bool(getattr(m.sticker, "is_animated", False))),
        ("premium_sticker", m.sticker is not None
         and getattr(m.sticker, "premium_animation", None) is not None),
        ("text", bool(text)),
        ("url", "url" in types or "text_link" in types
         or "url" in cap_types or "text_link" in cap_types),
        ("video", m.video is not None),
        ("videonote", m.video_note is not None),
        ("voice", m.voice is not None),
    )
    return any(flag for name, flag in checks if name in locks)


async def enforce_locks(message: Message, bot: Bot) -> None:
    """Delete messages that violate the chat's active locks."""
    if message.chat.type == "private":
        return
    user = message.from_user
    if not user:  # service/anonymous posts without a user (boa parity)
        return
    if user.is_bot or user.id == bot.id:
        return  # never delete the bot's own announcements
    locks = await adb(db.get_locks(message.chat.id))
    if not locks:
        return
    if await _is_admin(message, bot):
        return
    if not _violates(set(locks), message):
        return
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"lock delete failed in {message.chat.id}: {e}")


# ── Commands ─────────────────────────────────────────────────────
async def lock_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
                         parse_mode=ParseMode.HTML)
        return
    if not await _is_admin(message, bot):
        return
    if not args:
        await reply_text(
            message,
            f"{E.INFO} <b>Usage:</b> /lock &lt;type(s)&gt;\n"
            "Example: <code>/lock all</code> or <code>/lock audio video</code>\n"
            f"Use /locktypes to see every type.",
            parse_mode=ParseMode.HTML,
        )
        return
    types = [a.lower().lstrip("@") for a in args]
    for t in types:
        if t not in LOCKABLES:
            await reply_text(
                message,
                f"{E.ERROR} Invalid lock type: <code>{escape(t)}</code>\n"
                "Use /locktypes to view available lock types.",
                parse_mode=ParseMode.HTML,
            )
            return
    for t in types:
        await adb(db.set_lock(message.chat.id, t))
    await reply_text(
        message,
        f"{E.MUTE} <b>Locked:</b> <code>{escape(', '.join(types))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def unlock_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
                         parse_mode=ParseMode.HTML)
        return
    if not await _is_admin(message, bot):
        return
    if not args:
        await reply_text(
            message,
            f"{E.INFO} <b>Usage:</b> /unlock &lt;type(s)&gt;\n"
            "Example: <code>/unlock all</code> or <code>/unlock audio video</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    types = [a.lower().lstrip("@") for a in args]
    for t in types:
        if t not in LOCKABLES:
            await reply_text(
                message,
                f"{E.ERROR} Invalid lock type: <code>{escape(t)}</code>\n"
                "Use /locktypes to view available lock types.",
                parse_mode=ParseMode.HTML,
            )
            return
    for t in types:
        await adb(db.unset_lock(message.chat.id, t))
    await reply_text(
        message,
        f"{E.CHECK} <b>Unlocked:</b> <code>{escape(', '.join(types))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def locks_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
                         parse_mode=ParseMode.HTML)
        return
    locks = await adb(db.get_locks(message.chat.id))
    if not locks:
        await reply_text(
            message,
            f"{E.CHECK} <b>No locks are currently enabled in this chat.</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    await reply_text(
        message,
        f"{E.MUTE} <b>Active locks:</b>\n<code>{escape(', '.join(locks))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def locktypes_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
                         parse_mode=ParseMode.HTML)
        return
    items = list(LOCKABLES.items())
    rows = [
        [btn_default(t.capitalize(), f"locktype:{t}")
         for t, _ in items[i:i + 3]]
        for i in range(0, len(items), 3)
    ]
    await reply_text(
        message,
        f"{E.SETTINGS} <b>Available Lock Types</b>\n"
        "Tap a button to view its description.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_keyboard(rows),
    )


async def locktype_callback(query: CallbackQuery) -> None:
    lock_type = (query.data or "").split(":", 1)[1]
    description = LOCKABLES.get(lock_type, "No description available.")
    await query.answer(
        text=f"{E.MUTE} {lock_type.capitalize()} Lock:\n{description}",
        show_alert=True,
    )


# ── Setup ────────────────────────────────────────────────────────
def setup() -> list:
    on("message", lock_command, flt=and_f(cmd("lock"), GROUPS))
    on("message", unlock_command, flt=and_f(cmd("unlock"), GROUPS))
    on("message", locks_command, flt=and_f(cmd("locks"), GROUPS))
    on("message", locktypes_command, flt=and_f(cmd("locktypes"), GROUPS))
    on(
        "callback_query",
        locktype_callback,
        flt=F.data.regexp(re.compile(r"^locktype:")),
    )
    # Enforcement: after flood counters? No — BEFORE them, so locked
    # messages never feed XP/burst stats. Group 4 (leveling is 5).
    on(
        "message",
        enforce_locks,
        group=4,
        flt=GROUPS & ~SERVICE,
    )
    return ["/lock", "/unlock", "/locks", "/locktypes", "lock-enforce"]
