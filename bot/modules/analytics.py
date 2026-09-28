"""Analytics — community insight: messages, members, peaks, moderation, spam.

Commands (groups):
  /stats [day|week|month]  — analytics dashboard (admin)
  /topactive [day|week]    — most active users
  /peakhours [days]        — busiest hours

Tracking hooks (message hour + join/leave counters) run in low-priority groups.
"""

import asyncio
import logging
from html import escape

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.command_handler import COMMAND
from bot.pipeline import GROUPS, SERVICE, cmd, on
from bot.reply import reply_text
from bot.database import db
from bot.emojis import E
from bot.responses import action_card, field_extra
from bot.timeutils import ist_date, ist_monday, ist_month_start

logger = logging.getLogger(__name__)

_PERIOD_DAYS = {"day": 1, "today": 1, "week": 7, "month": 30}


async def _is_admin(message: Message, bot: Bot) -> bool:
    try:
        member = await bot.get_chat_member(
            message.chat.id, message.from_user.id
        )
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _fmt(n: int) -> str:
    return f"{n:,}"


def _resolve_days(args, default: int = 1) -> int:
    if not args:
        return default
    key = args[0].lower()
    if key in _PERIOD_DAYS:
        return _PERIOD_DAYS[key]
    try:
        return max(1, min(365, int(key)))
    except ValueError:
        return default


# ── Tracking ────────────────────────────────────────────

async def track_message_analytics(
    message: Message
) -> None:
    """Increment daily + hourly message counters (off the hot path)."""
    if not message or message.chat.type == "private":
        return
    user = message.from_user
    if not user or user.is_bot:
        return

    chat_id = message.chat.id
    hour = message.date.hour if message.date else 0

    def _db() -> None:
        try:
            db.bump_messages(chat_id, 1)
            db.bump_hourly(chat_id, hour, 1)
        except Exception as e:
            logger.warning(f"analytics message track failed: {e}")

    import asyncio
    asyncio.get_running_loop().run_in_executor(None, _db)


async def track_join(
    message: Message, bot: Bot
) -> None:
    if not message or not message.new_chat_members:
        return
    if message.chat.type == "private":
        return
    chat_id = message.chat.id
    n = sum(1 for m in message.new_chat_members if not m.is_bot and m.id != bot.id)
    if n <= 0:
        return

    def _db() -> None:
        try:
            db.bump_new_members(chat_id, n)
        except Exception as e:
            logger.warning(f"analytics join track failed: {e}")

    import asyncio
    asyncio.get_running_loop().run_in_executor(None, _db)


async def track_leave(
    message: Message, bot: Bot
) -> None:
    if not message or not message.left_chat_member:
        return
    if message.chat.type == "private":
        return
    user = message.left_chat_member
    if user.is_bot or user.id == bot.id:
        return
    chat_id = message.chat.id

    def _db() -> None:
        try:
            db.bump_left_members(chat_id, 1)
        except Exception as e:
            logger.warning(f"analytics leave track failed: {e}")

    import asyncio
    asyncio.get_running_loop().run_in_executor(None, _db)


# ── Commands ────────────────────────────────────────────

async def stats_command(message: Message, bot: Bot, args: list):
    """/stats — full analytics dashboard for this chat."""
    if message.chat.type == "private":
        await reply_text(message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(message,
            f"{E.ERROR} Only admins can view analytics.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = message.chat.id
    period = "day"
    if args and args[0].lower() in _PERIOD_DAYS:
        period = args[0].lower()
    days = _PERIOD_DAYS.get(period, 1)

    stats = db.get_daily_stats(chat_id, days=days)
    # Prefer daily_messages for message volume (leveling already tracks it).
    from datetime import date, timedelta as td
    start = (date.today() - td(days=days - 1)).isoformat()
    try:
        msg_sum = db.sum_daily_messages(chat_id, start)
    except Exception:
        msg_sum = stats.get("messages", 0)

    active = db.get_active_member_count(chat_id, days=days)
    label = period.capitalize()

    # Trend: compare to previous equal window when we have history.
    trend = "—"
    if days >= 1:
        prev_start = (date.today() - td(days=days * 2 - 1)).isoformat()
        try:
            prev = db.sum_daily_messages(chat_id, prev_start, start)
            if prev > 0:
                delta = msg_sum - prev
                pct = int(abs(delta) / prev * 100)
                trend = f"{'▲' if delta >= 0 else '▼'} {pct}% vs prior"
            elif msg_sum > 0:
                trend = "NEW"
        except Exception:
            pass

    fields = [
        field_extra(E.FIRE, "Messages", f"{_fmt(msg_sum)} ({label})"),
        field_extra(E.INFO, "Trend", escape(trend)),
        field_extra(E.USER, "Active Members", str(active)),
        field_extra(E.NEW, "New Members", str(stats.get("new_members", 0))),
        field_extra(E.GOODBYE, "Left Members", str(stats.get("left_members", 0))),
        field_extra(E.WARN, "Mod Actions", str(stats.get("mod_actions", 0))),
        field_extra(E.CROSS, "Spam Attempts", str(stats.get("spam_attempts", 0))),
        field_extra(E.WEB, "Bind Fails", str(stats.get("bind_fails", 0))),
    ]
    text = action_card("Analytics Dashboard", fields, icon=E.SETTINGS)
    await reply_text(message, text, parse_mode=ParseMode.HTML)


async def topactive_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(message,
            f"{E.ERROR} Only admins can view analytics.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = message.chat.id
    period = "week"
    if args and args[0].lower() in _PERIOD_DAYS:
        period = args[0].lower()

    # Same IST windows as /rankings: day = today, week = Monday,
    # month = calendar month (all refresh on IST boundaries).
    since = {
        "day": ist_date(),
        "today": ist_date(),
        "week": ist_monday(),
        "month": ist_month_start(),
    }.get(period, ist_monday())

    top = await asyncio.to_thread(
        db.get_period_top, chat_id, since=since, limit=10
    )
    if not top:
        await reply_text(message,
            f"{E.INFO} No activity recorded yet.", parse_mode=ParseMode.HTML
        )
        return

    medals = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3]
    lines = [f"{E.FIRE} <b>Top Active ({period.capitalize()})</b>", ""]
    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"{i+1}."
        total = int(entry.get("total_messages") or 0)
        lines.append(f"{medal} <b>{escape(str(name))}</b> — {_fmt(total)}")
    await reply_text(message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def peakhours_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message,
            f"{E.INFO} This command only works in groups.", parse_mode=ParseMode.HTML
        )
        return
    if not await _is_admin(message, bot):
        await reply_text(message,
            f"{E.ERROR} Only admins can view analytics.", parse_mode=ParseMode.HTML
        )
        return

    chat_id = message.chat.id
    days = 7
    if args:
        try:
            days = max(1, min(90, int(args[0])))
        except ValueError:
            pass

    peaks = db.get_peak_hours(chat_id, days=days, limit=5)
    if not peaks:
        await reply_text(message,
            f"{E.INFO} No hourly data yet.", parse_mode=ParseMode.HTML
        )
        return

    lines = [f"{E.TIME} <b>Peak Hours ({days}d)</b>", ""]
    for i, p in enumerate(peaks):
        h = int(p["hour"])
        medal = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3][i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <code>{h:02d}:00</code> — {_fmt(int(p['messages']))} msgs")
    await reply_text(message, "\n".join(lines), parse_mode=ParseMode.HTML)


# ── Setup ───────────────────────────────────────────────

def setup() -> list:
    group_filter = GROUPS

    on("message", stats_command, flt=and_f(cmd("stats"), group_filter))
    on("message", stats_command, flt=and_f(cmd("analytics"), group_filter))
    on("message", topactive_command, flt=and_f(cmd("topactive"), group_filter))
    on("message", peakhours_command, flt=and_f(cmd("peakhours"), group_filter))

    # Message hour counter — after leveling(5)/security(6) → group 7.
    on(
        "message",
        track_message_analytics,
        group=7,
        flt=and_f(group_filter, ~COMMAND, ~SERVICE),
    )
    # Join/leave — after security joins(12) → group 13.
    on(
        "message",
        track_join,
        group=13,
        flt=group_filter & F.new_chat_members,
    )
    on(
        "message",
        track_leave,
        group=13,
        flt=group_filter & F.left_chat_member,
    )

    return ["/stats", "/analytics", "/topactive", "/peakhours", "trackers"]
