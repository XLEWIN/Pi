"""Owner-only /mychats — paged inline menu of the bot's admin chats.

* ``/mychats``    — scans every tracked group, keeps the ones where THIS
  bot currently holds admin rights, and shows them as green
  buttons: 5 chats per page (one per row) + a prev/next row.
* a chat button   — swaps in an info card in the owner's exact
  small-caps template (name / id / username / members / link),
  with a ready-to-join invite link whenever the bot may
  create one (creator rights or ``can_invite_users``).
* BACK           — returns to the list on the page you came from.

Owner-only at EVERY entry point: the command checks the sender, and the
``mychats:*`` callbacks check ``query.from_user`` — a leaked button press
from anyone else gets an alert and nothing else.

The admin scan hits Telegram once per tracked group, so its result is
cached in ``bot_data`` for ``_CACHE_TTL`` seconds: page flips reuse it,
``/mychats`` always rescans fresh.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import escape
from typing import Any, Dict, List, Optional

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.database import db
from bot.emojis import E
from bot.keyboards.colored import btn_success, build_keyboard
from bot.modules.users import _chat_link
from bot.responses import plain_error
from bot.async_bridge import adb

logger = logging.getLogger(__name__)

_CB = "mychats"
PAGE_SIZE = 5            # chats per page — one green button per row
_CACHE_KEY = "mychats_admin_chats"
_CACHE_TTL = 60.0        # seconds a scan stays fresh for page flips
_API_TIMEOUT = 8.0       # per-call cap so one dead chat can't stall us


# ── Small helpers ────────────────────────────────────────────────

def _is_owner(user) -> bool:
    """True only for the configured OWNER_ID (unset/0 → nobody)."""
    return bool(user is not None and settings.owner_id
                and user.id == settings.owner_id)


def _page_count(total: int) -> int:
    """Pages needed for ``total`` chats — always at least 1."""
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _clamp(page: int, total: int) -> int:
    return max(0, min(page, _page_count(total) - 1))


def _int_at(parts: List[str], index: int) -> int:
    try:
        return int(parts[index])
    except (IndexError, ValueError):
        return 0


def _button_label(title: str) -> str:
    """Chat title trimmed to Telegram's 64-byte button-text cap."""
    raw = str(title).strip() or "Unnamed"
    data = raw.encode("utf-8")
    if len(data) <= 64:
        return raw
    # "…" is 3 bytes → cut at 61, never split a code point.
    return data[:61].decode("utf-8", "ignore").rstrip() + "\u2026"


# ── Menu text + keyboard (bot's tree style, E.* icons only) ──────

def _menu_text(chats: List[Dict[str, Any]], page: int) -> str:
    total = len(chats)
    if total == 0:
        return (
            f"{E.ANNOUNCE} My Chats\n"
            f"\u251c {E.WARN} Admin chats: 0\n"
            f"\u2514 {E.INFO} Add me as an admin somewhere, "
            "then run /mychats again"
        )
    pages = _page_count(total)
    return (
        f"{E.ANNOUNCE} My Chats\n"
        f"\u251c {E.ADMIN} Admin chats: {total}\n"
        f"\u2514 {E.INFO} Page: {page + 1}/{pages}"
    )


def _menu_keyboard(chats: List[Dict[str, Any]], page: int):
    """Up to 5 green chat rows + a green prev/indicator/next row."""
    pages = _page_count(len(chats))
    start = page * PAGE_SIZE
    chunk = chats[start : start + PAGE_SIZE]
    rows = [
        [
            btn_success(
                _button_label(c.get("title") or "Unnamed"),
                f"{_CB}:info:{c['chat_id']}:{page}",
            )
        ]
        for c in chunk
    ]
    nav = []
    if page > 0:
        nav.append(btn_success("<", f"{_CB}:page:{page - 1}"))
    if pages > 1:
        nav.append(btn_success(f"{page + 1}/{pages}", f"{_CB}:noop"))
    if page < pages - 1:
        nav.append(btn_success(">", f"{_CB}:page:{page + 1}"))
    if nav:
        rows.append(nav)
    return build_keyboard(rows)


def _back_keyboard(page: int):
    return build_keyboard([[btn_success("BACK", f"{_CB}:page:{page}")]])


def _info_card(chat, members: str, link: str) -> str:
    """The owner's exact small-caps info template."""
    title = escape(str(chat.title or getattr(chat, "first_name", None)
                        or "Unnamed"))
    username = getattr(chat, "username", None)
    uname = f"@{escape(username)}" if username else "No username"
    return (
        f"ᴄʜᴀᴛ ɴᴀᴍᴇ : {title}\n"
        f"ᴄʜᴀᴛ ɪᴅ : {chat.id}\n"
        f"ᴄʜᴀᴛ ᴜsᴇʀɴᴀᴍᴇ : {uname}\n"
        f"ɢʀᴏᴜᴘ ᴍᴇᴍʙᴇʀs : {members}\n"
        f"ᴄʜᴀᴛ ʟɪɴᴋ : {escape(str(link))}"
    )


# ── Admin scan (live) + short-lived cache ────────────────────────

async def _scan_admin_chats(bot: Bot) -> List[Dict[str, Any]]:
    """Tracked groups where THIS bot is currently admin/creator."""
    rows = await adb(db.get_all_groups())
    if not rows:
        return []

    async def _keep(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cid = row.get("chat_id")
        if cid is None:
            return None
        try:
            member = await asyncio.wait_for(
                bot.get_chat_member(cid, bot.id), timeout=_API_TIMEOUT
            )
        except Exception:
            return None  # gone / not admin / API error → not listed
        if member.status in ("administrator", "creator"):
            return {
                "chat_id": cid,
                "title": row.get("chat_title") or "Unnamed",
            }
        return None

    kept = await asyncio.gather(*(_keep(r) for r in rows))
    return [k for k in kept if k is not None]


def _store_cache(bot_data: dict, chats: List[Dict[str, Any]]) -> None:
    bot_data[_CACHE_KEY] = {"at": time.time(), "chats": chats}


def _cached(bot_data: dict) -> Optional[List[Dict[str, Any]]]:
    entry = bot_data.get(_CACHE_KEY)
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("at", 0)) > _CACHE_TTL:
        return None
    chats = entry.get("chats")
    return chats if isinstance(chats, list) else None


async def _fresh_chats(bot: Bot, bot_data: dict) -> List[Dict[str, Any]]:
    chats = await _scan_admin_chats(bot)
    _store_cache(bot_data, chats)
    return chats


# ── Send / edit / answer helpers ─────────────────────────────────

async def _answer(query, text: Optional[str] = None, *,
                  alert: bool = False) -> None:
    try:
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()
    except Exception:
        pass


async def _safe_edit(query, text: str, markup) -> None:
    try:
        await query.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception as e:
        if type(e).__name__ in {"TimedOut", "NetworkError"}:
            logger.warning("mychats edit network issue: %s", e)
            return
        if "message is not modified" in str(e):
            return
        logger.warning("mychats edit failed: %s", e)


# ── Handlers ─────────────────────────────────────────────────────

async def mychats_command(message: Message, bot: Bot, bot_data: dict) -> None:
    """/mychats — owner-only list of the bot's admin chats."""
    msg = message
    if msg is None:
        return
    if not _is_owner(message.from_user):
        await reply_text(msg, 
            f"{E.CROWN} Only the bot owner can use /mychats.",
            parse_mode=ParseMode.HTML,
        )
        return

    chats = await _fresh_chats(bot, bot_data)   # command always rescans
    await reply_text(msg, 
        _menu_text(chats, 0),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(chats, 0) if chats else None,
    )


async def _send_info(query, bot: Bot, chat_id: int, page: int) -> None:
    """Swap the list for one chat's info card (live data + invite link)."""
    try:
        chat = await asyncio.wait_for(
            bot.get_chat(chat_id), timeout=_API_TIMEOUT
        )
    except Exception as e:
        logger.warning("mychats: get_chat(%s) failed: %s", chat_id, e)
        await _safe_edit(
            query, plain_error("Could not load this chat."),
            _back_keyboard(page),
        )
        return

    members = "Unknown"
    try:
        count = await asyncio.wait_for(
            bot.get_chat_member_count(chat_id), timeout=_API_TIMEOUT
        )
        members = f"{count:,}"
    except Exception as e:
        logger.debug("mychats: member count failed: %s", e)

    # Prefer a ready-to-join invite link when the bot may create one;
    # otherwise fall back to the public/@ or t.me/c/ form.
    link = _chat_link(chat)
    try:
        me = await asyncio.wait_for(
            bot.get_chat_member(chat_id, bot.id), timeout=_API_TIMEOUT
        )
        can_invite = (me.status == "creator"
                      or getattr(me, "can_invite_users", False))
        if can_invite:
            link = await asyncio.wait_for(
                bot.exportChatInviteLink(chat_id), timeout=_API_TIMEOUT
            )
    except Exception as e:
        logger.debug("mychats: invite link unavailable: %s", e)

    await _safe_edit(query, _info_card(chat, members, link),
                     _back_keyboard(page))


async def mychats_callback(callback_query: CallbackQuery, bot: Bot, bot_data: dict) -> None:
    """Route mychats:* callbacks — pagination, info cards, no-ops."""
    query = callback_query
    if query is None or not str(query.data or "").startswith(f"{_CB}:"):
        return
    if not _is_owner(query.from_user):
        await _answer(query, "Only the bot owner can use this.",
                      alert=True)
        return

    parts = str(query.data).split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await _answer(query)
        return

    # Page flips and info cards share the scan; refresh if it expired.
    chats = _cached(bot_data)
    if chats is None:
        chats = await _fresh_chats(bot, bot_data)

    if action == "page":
        page = _clamp(_int_at(parts, 2), len(chats))
        await _answer(query)
        await _safe_edit(query, _menu_text(chats, page),
                         _menu_keyboard(chats, page))
        return

    if action == "info":
        chat_id = _int_at(parts, 2)
        page = _clamp(_int_at(parts, 3), len(chats))
        await _answer(query)
        await _send_info(query, bot, chat_id, page)
        return

    await _answer(query, "Unknown option", alert=True)


def setup() -> List[str]:
    """Register this module's handlers. Returns route descriptions."""
    on("message", mychats_command, flt=cmd("mychats"))
    on(
        "callback_query", mychats_callback,
        flt=F.data.regexp(re.compile(rf"^{_CB}:")),
    )
    return ["/mychats"]
