"""Nightmode — ported from the boa reference bot, restyled for Pi.

/nightmode toggles per-chat nightmode (admin, groups). While enabled:
- 11:00 PM IST the chat switches to text-only permissions,
- 07:00 AM IST normal permissions are restored.
A watcher task flips the permissions on the 23:00/07:00 transitions;
restarts during the night re-apply silently (no duplicate announcement).
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import CallbackQuery, ChatPermissions, Message

from bot.async_bridge import adb
from bot.database import db
from bot.emojis import E, EID, plain
from bot.keyboards.colored import btn_danger, btn_success, build_keyboard
from bot.logger import logger
from bot.modules.security import _is_admin, _member_is_admin
from bot.pipeline import GROUPS, cmd, on
from bot.reply import reply_text
from bot.timeutils import ist_now

#: Night: text only (boa parity — every media/extra permission off).
NIGHT = ChatPermissions(
    can_send_messages=True,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

#: Day: everything allowed again.
DAY = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)

POLL_SECONDS = 30
_STATUS_ON = (
    f"{E.CLOCK} <b>Nightmode is enabled in this chat.</b>\n"
    "Media is muted at <b>11:00 PM IST</b> and restored at "
    "<b>7:00 AM IST</b>."
)
_STATUS_OFF = (
    f"{E.CLOCK} <b>Nightmode is disabled in this chat.</b>\n"
    "Enable it to mute everything but text overnight (IST)."
)

_watcher: Optional[asyncio.Task] = None


# ── Scheduling core ──────────────────────────────────────────────
def _phase(dt) -> str:
    """Current IST phase: night between 23:00 and 07:00, else day."""
    return "night" if dt.hour >= 23 or dt.hour < 7 else "day"


async def _apply(
    bot: Bot, chats: List[int], *, night: bool, announce: bool
) -> None:
    """Flip permissions for every opted-in chat (best effort)."""
    perms = NIGHT if night else DAY
    notice = (
        f"{E.CLOCK} <b>Nightmode</b> — text-only until 7:00 AM."
        if night
        else f"{E.CHECK} <b>Nightmode</b> — normal permissions restored."
    )
    for chat_id in chats:
        try:
            await bot.set_chat_permissions(chat_id, perms)
            if announce:
                await bot.send_message(
                    chat_id, notice, parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.warning(f"nightmode apply failed for {chat_id}: {e}")
        await asyncio.sleep(1)


async def watch(bot: Bot) -> None:
    """Background loop: flip on 23:00/07:00 IST transitions."""
    prev = _phase(ist_now())
    if prev == "night":
        # Restarted mid-night: re-apply quietly so a bot that was down
        # at 23:00 doesn't leave the chat wide open all night.
        chats = await adb(db.get_nightmode_chats())
        if chats:
            await _apply(bot, chats, night=True, announce=False)
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            cur = _phase(ist_now())
            if cur == prev:
                continue
            prev = cur
            chats = await adb(db.get_nightmode_chats())
            if not chats:
                continue
            await _apply(bot, chats, night=(cur == "night"), announce=True)
        except Exception as e:
            logger.warning(f"nightmode watch iteration failed: {e}")


def start(bot: Bot) -> None:
    """Spawn the watcher once (called from main startup)."""
    global _watcher
    if _watcher is not None and not _watcher.done():
        return
    _watcher = asyncio.create_task(watch(bot))
    _watcher.add_done_callback(_watcher_done)


def _watcher_done(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception():
        logger.error(f"nightmode watcher died: {t.exception()}")


# ── Commands ─────────────────────────────────────────────────────
def _toggle_keyboard(enabled: bool):
    if enabled:
        rows = [[btn_danger("Disable Nightmode", "nm:off",
                            icon_emoji_id=EID.CLOCK)]]
    else:
        rows = [[btn_success("Enable Nightmode", "nm:on",
                             icon_emoji_id=EID.CLOCK)]]
    return build_keyboard(rows)


async def nightmode_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(
            message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not await _is_admin(message, bot):
        return
    enabled = await adb(db.is_nightmode(message.chat.id))
    await reply_text(
        message,
        _STATUS_ON if enabled else _STATUS_OFF,
        parse_mode=ParseMode.HTML,
        reply_markup=_toggle_keyboard(enabled),
    )


async def nightmode_callback(query: CallbackQuery, bot: Bot) -> None:
    data = query.data or ""
    message = query.message
    if message is None or message.chat.type == "private":
        # Toasts have no parse_mode — strip the <tg-emoji> markup first.
        await query.answer(plain(f"{E.ERROR} Groups only."), show_alert=True)
        return
    user = query.from_user
    if (
        not user
        or await _member_is_admin(bot, message.chat.id, user.id) is not True
    ):
        await query.answer()
        return
    enable = data == "nm:on"
    await adb(db.set_nightmode(message.chat.id, enable))
    await query.answer(
        plain(f"{E.CHECK} Nightmode {'enabled' if enable else 'disabled'}.")
    )
    try:
        await message.edit_text(
            _STATUS_ON if enable else _STATUS_OFF,
            parse_mode=ParseMode.HTML,
            reply_markup=_toggle_keyboard(enable),
        )
    except Exception as e:
        logger.warning(f"nightmode button edit failed: {e}")


# ── Setup ────────────────────────────────────────────────────────
def setup() -> list:
    on("message", nightmode_command, flt=and_f(cmd("nightmode"), GROUPS))
    on(
        "callback_query",
        nightmode_callback,
        flt=F.data.regexp(re.compile(r"^nm:(on|off)$")),
    )
    return ["/nightmode", "nm-callback"]
