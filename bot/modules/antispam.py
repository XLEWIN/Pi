"""Anti-flood — 5 messages in 3 seconds ⇒ timed block, escalating.

* Window: per (chat, user) — 5 messages inside 3 seconds triggers.
* On flood the bot warns in that chat (Pi style, owner's emoji only):
      ❌ <mention> is flooding: blocked for 5 minutes for using the bot.
* Offences escalate within the 24h refresh (IST midnight):
      1st → 5 min, 2nd → 10 min, 3rd and beyond → 20 min.
* While blocked, the user's messages are ignored by every counting
  handler (chatstats + leveling) — no ranks, no XP.
* ``/free`` (sudo) clears a user's warnings and active block.

Detection runs in its own handler group — **group 9** — and never in
group 0. PTB's dispatch runs at MOST ONE handler per group ("break —
Only a max of 1 handler per group is handled"), then moves to the next
group. Sharing group 0 with chatstats.count_message /
users.track_message made flood_watch the first match for every plain
group message, so the counter and registration never ran at all.
The loader registers antispam before chatstats, so group 9 is created
before the counter's group 18 and the block is already set when the
counter sees the triggering message.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Dict, List, Optional, Tuple

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.command_handler import COMMAND
from bot.pipeline import GROUPS, cmd, on
from bot.reply import reply_text
from bot.database import db
from bot.emojis import E
from bot.modules.bans import is_sudo
from bot.responses import mention, plain_error, plain_ok
from bot.timeutils import ist_date

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 3.0   # flood window
_FLOOD_AT = 5           # messages inside the window that trigger
_OFFENCE_MINUTES = (5, 10, 20)   # 1st, 2nd, 3rd+ (capped at 20)

# (chat_id, user_id) → message timestamps inside the window (time.time()).
_WINDOWS: Dict[Tuple[int, int], deque] = {}


def _track(chat_id: int, user_id: int, now: Optional[float] = None) -> bool:
    """Record one message; True when the flood threshold is hit."""
    now = time.time() if now is None else now
    key = (chat_id, user_id)
    window = _WINDOWS.setdefault(key, deque())
    window.append(now)
    while window and now - window[0] > _WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _FLOOD_AT:
        window.clear()
        return True
    return False


def reset_windows() -> None:
    """Test helper — drop every in-flight flood window."""
    _WINDOWS.clear()


def block_minutes(offence: int) -> int:
    """Escalation: 1st 5 min, 2nd 10 min, 3rd+ 20 min."""
    return _OFFENCE_MINUTES[min(offence, len(_OFFENCE_MINUTES)) - 1]


def flood_text(user, minutes: int) -> str:
    """Pi-style warning: ❌ <mention> is flooding: blocked for N minutes…"""
    return (
        f"{E.ERROR} {mention(user)} is flooding: blocked for "
        f"{minutes} minutes for using the bot."
    )


# ═════════════════════════════════════════════════════════════════
# Flood watcher — same filter as the counter (group text messages)
# ═════════════════════════════════════════════════════════════════

async def flood_watch(message: Message) -> None:
    if not message or not message.text:
        return
    chat = message.chat
    user = message.from_user
    if chat is None or chat.type == "private" or user is None or user.is_bot:
        return

    # Already blocked: ignore (no new offence while the block runs).
    if await asyncio.to_thread(db.is_spam_blocked, user.id):
        return

    if not _track(chat.id, user.id):
        return

    offence = await asyncio.to_thread(db.spam_bump_offence, user.id, ist_date())
    minutes = block_minutes(offence)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await asyncio.to_thread(
        db.spam_set_block, user.id, until.isoformat(timespec="seconds")
    )

    try:
        await reply_text(message,
            flood_text(user, minutes), parse_mode=ParseMode.HTML
        )
    except Exception as e:  # noqa: BLE001 — warning must never crash the handler
        logger.warning("flood warning failed: %s", e)


# ═════════════════════════════════════════════════════════════════
# /free — sudo only: clear warnings + block
# ═════════════════════════════════════════════════════════════════

def _label_from_row(row: dict) -> str:
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return escape(full or (row.get("username") or "").strip()
                  or str(row.get("user_id", "?")))


def _resolve_target(message: Message, args: list):
    """(user_id, display_label) from reply / @username / USER_ID — or None."""
    msg = message
    if msg is not None and msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
        return target.id, escape(target.username or target.first_name or str(target.id))
    if args:
        arg = args[0]
        if arg.startswith("@"):
            row = db.get_user_by_username(arg[1:])
            if row:
                return row["user_id"], _label_from_row(row)
            return None
        try:
            user_id = int(arg)
        except ValueError:
            return None
        row = db.get_user(user_id)
        if row:
            return row["user_id"], _label_from_row(row)
        return user_id, escape(str(user_id))
    return None


async def free_command(message: Message, args: list) -> None:
    msg = message
    if msg is None:
        return
    sender = message.from_user
    if sender is None or not await asyncio.to_thread(is_sudo, sender.id):
        await reply_text(msg,
            f"{E.ERROR} Only sudo/owner users can use /free.",
            parse_mode=ParseMode.HTML,
        )
        return
    target = await asyncio.to_thread(_resolve_target, message, args)
    if target is None:
        await reply_text(msg,
            plain_error("Usage: reply to a user, or /free @username | /free USER_ID"),
            parse_mode=ParseMode.HTML,
        )
        return
    user_id, label = target
    if await asyncio.to_thread(db.spam_clear, user_id):
        await reply_text(msg,
            plain_ok(f"<b>{label}</b> is free — warnings and block cleared."),
            parse_mode=ParseMode.HTML,
        )
    else:
        await reply_text(msg,
            f"{E.INFO} {label} has no active warnings or block.",
            parse_mode=ParseMode.HTML,
        )


# ═════════════════════════════════════════════════════════════════

def setup() -> List[str]:
    # Own group (9): one handler per group in PTB — must not shadow
    # chatstats(18)/users(19)/leveling(5) in group 0.
    on(
        "message",
        flood_watch,
        group=9,
        flt=and_f(F.text, ~COMMAND, GROUPS),
    )
    on("message", free_command, flt=cmd("free"))
    return ["/free", "flood_watch"]
