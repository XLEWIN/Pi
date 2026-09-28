"""AFK mode - ported from the boa reference bot, restyled for Pi.

* ``/afk [reason]`` (also ``/brb``, or plain-text ``off`` / ``brb``)
  toggles AFK: the first use stores reason + optional replied media,
  the second use clears it with a welcome-back card and the away time.
* A reply, text-mention or ``@username`` of an AFK user posts a
  Pi-branded notice with the away duration, reason and (if set) their
  media - same coverage as boa's afk_mention_handler.
* The next message an AFK user sends clears the status and welcomes
  them back - boa's auto-return, same three-way race handling (the
  toggle texts are excluded from the return handler so ``/afk`` never
  immediately clears what it just set).

Text style: action cards + the owner's E.* custom emoji set (never
stock glyphs); the random flavour lines are plain text under the card.
Storage: the ``afk`` collection (bot/database.py).

Dispatch groups: the toggle lives in group 0 with the other commands;
mention/return get their own new groups 22/23 so neither can shadow
the other (see tests/test_dispatch_groups.py for the group map).
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from html import escape
from typing import List, Optional, Tuple

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import or_f
from aiogram.types import Message

from bot.database import db
from bot.emojis import E
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.responses import action_card

# New handler groups - 0..21 are taken by the other modules.
MENTION_GROUP = 22  # ping/reply notices for AFK users
RETURN_GROUP = 23   # auto-clear when the AFK user speaks again

# Plain-text toggles (boa parity): "off", "off reason", "brb", "brb bgmi".
_TOGGLE_RE = re.compile(r"(?i)^(?:off|brb)(?:\s+.*)?$")


def _toggle_filter():
    """Command or plain-text toggle (shared by set + return exclusion)."""
    return or_f(cmd("afk", "brb"), F.text.regexp(_TOGGLE_RE))


# Media kinds a replied message can carry, in detection order.
_MEDIA_KINDS: Tuple[str, ...] = (
    "photo", "video", "animation", "audio", "voice", "document", "video_note",
)

# Notification methods for stored media (video_note has no caption and
# is sent first, with the card as a follow-up message).
_CAPTION_SEND = {
    "photo": "send_photo",
    "video": "send_video",
    "animation": "send_animation",
    "audio": "send_audio",
    "voice": "send_voice",
    "document": "send_document",
}

# Flavour lines (plain text under the Pi card - E.* lives in the card).
_SET_LINES: Tuple[str, ...] = (
    "Back in a bit - ping again if anything is urgent.",
    "Stepped away for a moment; replies will follow.",
    "Away right now. Drop a message and it will be seen later.",
    "Taking a short break - catch you soon!",
    "Gone AFK. Messages will be answered after the return.",
    "Out for a while - keep the chat warm!",
)
_REPLY_LINES: Tuple[str, ...] = (
    "They will get back to you when they return.",
    "Try pinging again in a few minutes.",
    "Away mode is on - replies come after the comeback.",
    "No reply from an AFK ghost - give them a moment.",
)
_BACK_LINES: Tuple[str, ...] = (
    "Nice to see you again!",
    "Welcome back to the chat!",
    "The AFK spell has ended - carry on!",
    "Back online and ready to roll.",
    "Reported for duty!",
    "Did you bring snacks back?",
)


def _mention(user_id: int, name: str) -> str:
    """First-name profile mention (same shape as chatstats boards)."""
    return f'<a href="tg://user?id={user_id}">{escape(str(name))}</a>'


def _sender_mention(user) -> str:  # noqa: ANN001
    name = (
        (getattr(user, "first_name", None) or "").strip()
        or (getattr(user, "username", None) or "").strip()
        or f"User {getattr(user, 'id', '?')}"
    )
    return _mention(user.id, name)


def _display_name(doc: dict) -> str:
    return (
        (doc.get("user_first_name") or "").strip()
        or (doc.get("username") or "").strip()
        or f"User {doc.get('user_id')}"
    )


def _duration_since(start_iso: Optional[str]) -> str:
    """Human away-time: "1 hour, 5 minutes" (boa's format_time_delta)."""
    try:
        delta = datetime.now() - datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        return "a while"
    total = max(int(delta.total_seconds()), 0)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: List[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return ", ".join(parts)


def _replied_media(message: Message) -> Tuple[Optional[str], Optional[str]]:
    """file_id + kind of the replied message's media (boa parity)."""
    rt = getattr(message, "reply_to_message", None)
    if rt is None:
        return None, None
    for kind in _MEDIA_KINDS:
        obj = getattr(rt, kind, None)
        file_id = getattr(obj, "file_id", None)
        if file_id:
            return file_id, kind
    return None, None


def _resolve_afk_target(message: Message) -> Optional[dict]:
    """AFK doc of the user this message points at (reply / text-mention /
    @username word), or None - the three paths boa's resolver covered."""
    rt = getattr(message, "reply_to_message", None)
    target = getattr(rt, "from_user", None) if rt is not None else None
    if target is not None and not getattr(target, "is_bot", False):
        doc = db.get_afk(target.id)
        if doc:
            return doc
    # Text-mention entities (names of users without a @username).
    for ent in getattr(message, "entities", None) or []:
        if getattr(ent, "type", None) == "text_mention":
            u = getattr(ent, "user", None)
            if u is not None:
                doc = db.get_afk(u.id)
                if doc:
                    return doc
    # Plain @username words anywhere in the message.
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    for word in text.split():
        if word.startswith("@") and len(word) > 1:
            doc = db.get_afk_by_username(word[1:])
            if doc:
                return doc
    return None


async def _welcome_back(message: Message, user, doc: dict) -> None:  # noqa: ANN001
    """Clear the AFK row and reply with the shared welcome-back card."""
    await asyncio.to_thread(db.clear_afk, user.id)
    fields = [
        (E.USER, "User", _sender_mention(user)),
        (E.TIME, "Away", _duration_since(doc.get("afk_start_time"))),
    ]
    text = "\n".join((
        action_card("Welcome Back", fields, icon=E.WELCOME),
        escape(random.choice(_BACK_LINES)),
    ))
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def afk_command(message: Message) -> None:
    """/afk, /brb and plain "off"/"brb" - toggle AFK on/off (boa parity)."""
    user = message.from_user
    if user is None or user.is_bot:
        return

    existing = await asyncio.to_thread(db.get_afk, user.id)
    if existing:
        await _welcome_back(message, user, existing)
        return

    # Reason = everything after the command/toggle word. Derived from the
    # raw text so it works for both the cmd filter and the plain regex
    # branches (the regex path injects no args).
    reason: Optional[str] = None
    parts = (message.text or "").strip().split(None, 1)
    if len(parts) > 1 and parts[1].strip():
        reason = parts[1].strip()

    media_id, media_type = _replied_media(message)
    await asyncio.to_thread(
        db.set_afk,
        user.id, user.first_name, user.username, reason,
        datetime.now().isoformat(), media_id, media_type,
    )

    fields = [(E.USER, "User", _sender_mention(user))]
    if reason:
        fields.append((E.INFO, "Reason", escape(reason)))
    text = "\n".join((
        action_card("AFK Mode Enabled", fields, icon=E.EYES),
        escape(random.choice(_SET_LINES)),
    ))
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def afk_mention_handler(message: Message, bot: Bot) -> None:
    """Reply/mention of an AFK user -> notice with duration + reason."""
    sender = message.from_user
    if sender is None or sender.is_bot:
        return

    doc = await asyncio.to_thread(_resolve_afk_target, message)
    if not doc or doc.get("user_id") == sender.id:
        return  # nobody AFK, or the sender pinging their own AFK row

    fields = [
        (E.USER, "User", _mention(doc["user_id"], _display_name(doc))),
        (E.TIME, "AFK for", _duration_since(doc.get("afk_start_time"))),
    ]
    if doc.get("afk_reason"):
        fields.append((E.INFO, "Reason", escape(doc["afk_reason"])))
    text = "\n".join((
        action_card("User Is Away", fields, icon=E.EYES),
        escape(random.choice(_REPLY_LINES)),
    ))

    media_id = doc.get("media_id")
    media_type = doc.get("media_type")
    if media_id and media_type == "video_note":
        try:
            await bot.send_video_note(message.chat.id, media_id)
        except Exception:  # noqa: BLE001 - expired file_id falls back
            pass
        await reply_text(message, text, parse_mode=ParseMode.HTML)
        return
    if media_id and media_type in _CAPTION_SEND:
        try:
            send = getattr(bot, _CAPTION_SEND[media_type])
            await send(
                message.chat.id, media_id,
                caption=text, parse_mode=ParseMode.HTML,
            )
            return
        except Exception:  # noqa: BLE001 - expired file_id falls back
            pass
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def afk_return_handler(message: Message) -> None:
    """First message from an AFK user clears the status (auto-return)."""
    user = message.from_user
    if user is None or user.is_bot:
        return
    doc = await asyncio.to_thread(db.get_afk, user.id)
    if not doc:
        return
    await _welcome_back(message, user, doc)


def setup() -> List[str]:
    """Register /afk + /brb toggles and the mention/return hooks."""
    toggle = _toggle_filter()
    on("message", afk_command, flt=toggle)
    on("message", afk_mention_handler, group=MENTION_GROUP)
    # Everything EXCEPT the toggle texts: "/afk" must not immediately
    # clear the row it just wrote (boa had the same exclusions).
    on("message", afk_return_handler, group=RETURN_GROUP, flt=~toggle)
    return [
        "/afk", "/brb",
        f"afk mention (group {MENTION_GROUP})",
        f"afk return (group {RETURN_GROUP})",
    ]
