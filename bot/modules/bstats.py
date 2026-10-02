"""Owner stats + latency — /bstats and /ping.

* ``/bstats`` — DB counters (users, chats, sudos, filters, rules,
  locks, gbans, gmutes) + uptime/started-at in the bot's tree style
  with E.* icons, green Refresh / red Close buttons.
* ``/ping``   — API round-trip latency (boa-style: measured around a
  real send) + uptime, with a green "Ping Again" button.

Both cards are pure HTML (``E.*`` custom emoji) and owner/anyone
gated as marked. Counts come from ``bot.database`` (Mongo) except
Total Rules — rules live in ``moderation.rules_db`` (in-memory).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.constants import BOT_START_TIME
from bot.database import db
from bot.emojis import E, EID
from bot.keyboards.colored import btn_danger, btn_success, build_keyboard
from bot.modules import moderation
from bot.pipeline import cmd, on
from bot.reply import reply_text

logger = logging.getLogger(__name__)

_CB_BSTATS = "bstats"
_CB_PING = "ping"
_STARTED_AT = datetime.fromtimestamp(BOT_START_TIME, tz=timezone.utc).strftime(
    "%Y-%m-%d %H:%M:%S UTC"
)


# ── Helpers ──────────────────────────────────────────────────────

def _is_owner(user) -> bool:
    return bool(user is not None and settings.owner_id
                and user.id == settings.owner_id)


def _fmt_uptime(seconds: int) -> str:
    """``1d 1h 28m 10s`` — zero units dropped, ``0s`` when fresh."""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s"))
             if n]
    return " ".join(parts) if parts else "0s"


def _uptime() -> str:
    return _fmt_uptime(time.time() - BOT_START_TIME)


def _count_rules() -> int:
    """Chats with /setrules text — moderation keeps rules in-memory."""
    return sum(1 for r in moderation.rules_db.values() if r.get("text"))


def _counts() -> Dict[str, Any]:
    """Fresh snapshot of every number /bstats shows (patchable seam)."""
    lock_chats, lock_total = db.get_lock_stats()
    return {
        "users": db.get_user_count(),
        "chats": db.get_group_count(),
        "sudos": len(db.get_sudo_users()),
        "filters": db.count_filters(),
        "rules": _count_rules(),
        "lock_chats": lock_chats,
        "locks": lock_total,
        "gbanned": len(db.get_gbanned_users()),
        "gmuted": db.count_gmuted(),
        "uptime": _uptime(),
        "started_at": _STARTED_AT,
    }


# ── Cards (bot's tree style, E.* icons only) ─────────────────────

def bstats_text(c: Dict[str, Any]) -> str:
    """The /bstats card — labels exactly as the owner specified."""
    return (
        f"{E.SAVE} Database Stats\n"
        f"├ {E.USER} Total Users: {c['users']}\n"
        f"├ {E.ANNOUNCE} Total Chats: {c['chats']}\n"
        f"├ {E.CROWN} Total Sudos: {c['sudos']}\n"
        f"├ {E.EYES} Total Filters: {c['filters']}\n"
        f"├ {E.BOOKMARK} Total Rules: {c['rules']}\n"
        f"├ {E.SETTINGS} Lock Stats:\n"
        f"   - Chats with Locks: {c['lock_chats']}\n"
        f"   - Total Locks: {c['locks']}\n"
        f"├ {E.BAN} GBanned Users: {c['gbanned']}\n"
        f"└ {E.MUTE} GMuted Users: {c['gmuted']}\n"
        "\n"
        f"{E.TIME} Time Details\n"
        f"├ {E.CLOCK} Uptime: {c['uptime']}\n"
        f"└ {E.NEW} Started At: {c['started_at']}"
    )


def _bstats_kb():
    return build_keyboard([
        [btn_success("Refresh", f"{_CB_BSTATS}:refresh", EID.CLOCK)],
        [btn_danger("Close", f"{_CB_BSTATS}:close", EID.CROSS)],
    ])


def ping_text(ping_ms: float) -> str:
    return (
        f"{E.CHECK} Pong!\n"
        f"├ {E.TIME} Ping: {ping_ms:.2f} ms\n"
        f"└ {E.CLOCK} Uptime: {_uptime()}"
    )


def _ping_kb():
    return build_keyboard(
        [[btn_success("Ping Again", f"{_CB_PING}:again", EID.FIRE)]]
    )


# ── Send / edit helpers ─────────────────────────────────────────

async def _answer(query, text: str | None = None, *, alert: bool = False) -> None:
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
            logger.warning("bstats edit network issue: %s", e)
            return
        if "message is not modified" in str(e):
            return
        logger.warning("bstats edit failed: %s", e)


# ── /bstats ─────────────────────────────────────────────────────

async def bstats_command(message: Message) -> None:
    """/bstats — owner-only database + uptime card."""
    msg = message
    if msg is None:
        return
    if not _is_owner(message.from_user):
        return
    await reply_text(
        msg,
        bstats_text(await asyncio.to_thread(_counts)),
        parse_mode=ParseMode.HTML,
        reply_markup=_bstats_kb(),
    )


# ── /ping ───────────────────────────────────────────────────────

async def ping_command(message: Message) -> None:
    """/ping — API latency measured around a real send (boa-style)."""
    msg = message
    if msg is None:
        return
    start = time.perf_counter()
    sent = await reply_text(
        msg,
        f"{E.FORWARD} Pinging...", parse_mode=ParseMode.HTML
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    await sent.edit_text(
        ping_text(elapsed_ms),
        parse_mode=ParseMode.HTML,
        reply_markup=_ping_kb(),
    )


# ── Callbacks ───────────────────────────────────────────────────

async def stats_callback(callback_query: CallbackQuery,
                         bot: Bot) -> None:
    """Route bstats:* and ping:* callbacks (Refresh / Close / Again)."""
    query = callback_query
    if query is None:
        return
    data = str(query.data or "")
    owner = _is_owner(query.from_user)

    if data.startswith(f"{_CB_BSTATS}:"):
        if not owner:
            await _answer(query)
            return
        action = data.split(":")[1] if ":" in data else ""
        if action == "refresh":
            await _answer(query)
            await _safe_edit(
                query, bstats_text(await asyncio.to_thread(_counts)), _bstats_kb()
            )
            return
        if action == "close":
            await _answer(query)
            try:
                await query.message.delete()
            except Exception:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception as e:
                    logger.debug("bstats close failed: %s", e)
            return
        await _answer(query, "Unknown option", alert=True)
        return

    if data.startswith(f"{_CB_PING}:"):
        # Ping is public — the button keeps the original card style.
        action = data.split(":")[1] if ":" in data else ""
        if action == "again":
            await _answer(query)
            start = time.perf_counter()
            try:
                await bot.get_me()
            except Exception as e:
                logger.warning("ping re-measure failed: %s", e)
            elapsed_ms = (time.perf_counter() - start) * 1000
            await _safe_edit(query, ping_text(elapsed_ms), _ping_kb())
            return
        await _answer(query, "Unknown option", alert=True)
        return

    await _answer(query, "Unknown option", alert=True)


def setup() -> list[str]:
    """Register /bstats and /ping + their callbacks."""
    on("message", bstats_command, flt=cmd("bstats"))
    on("message", ping_command, flt=cmd("ping"))
    on(
        "callback_query", stats_callback,
        flt=F.data.regexp(re.compile(r"^(bstats|ping):")),
    )
    return ["/bstats", "/ping"]
