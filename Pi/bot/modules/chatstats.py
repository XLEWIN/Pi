"""Chat message rankings — count every group text message and rank it.

* Counting: every non-command text message in a group/supergroup
  increments ``daily_messages`` (chat, user, IST day) off the event loop.
  The sender is auto-registered into ``users`` on their first message —
  no /start required — cached into ``group_members`` for that group, and
  a one-time #Newuser log (Pi style, owner's emoji) goes to the log
  channel (bot/modules/start.py::send_newuser_log).
  Group titles are refreshed in ``groups`` on the fly so /mytop always
  shows current names. ``add_message_xp`` no longer writes this table
  (its XP cooldown would double-count). Spam-blocked users are skipped.
* ``/rankings`` — this group's leaderboard (Overall / Today / Weekly).
* ``/mytop`` — the sender's personal board: their groups ranked by how
  much they chatted in each (same tabs), headed by the same Chat/Global
  rank lines /rank and /profile show.
* Rank-ups: crossing CHAT_RANK_MESSAGES (100) in a group or
  GLOBAL_RANK_MESSAGES (250) overall posts an announcement (the ladder
  from bot/constants.py — same numbers every rank surface derives).

Windows (IST — see bot/timeutils.py):
  Overall  all time
  Today    current IST day — refreshes at IST midnight
  Weekly   current IST week starting Monday — refreshes Monday 00:00 IST

Every 100 messages (100, then every 500) crossed in a chat per IST
day posts a milestone with the IST time it was reached. A milestone
landing on a rank-up threshold sends ONE combined reply:

    🏆 @user reached Rank 2 in this group!
    🔥 100 messages reached today! (14:08)

Board layout follows the requested format with Pi branding (all icons
are the owner's custom emoji set — <tg-emoji>, never plain glyphs).
The header always shows the selected scope (Overall / Today / Weekly),
matching the inline tabs:

    ⭐ Leaderboard · Overall
    1. 👤 <mention> • 11,797
    ━━━━━━━━━━━━━━
    📣 Total messages: 80,788

Buttons: active tab alone on row 1 (green + ✅), the others blue below.
Each tab carries the owner's custom emoji as its button icon
(icon_custom_emoji_id via EID — button text itself is plain-only).
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import List, Optional, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.command_handler import COMMAND, CommandHandler
from bot.database import db
from bot.constants import CHAT_RANK_MESSAGES, GLOBAL_RANK_MESSAGES
from bot.emojis import E, EID
from bot.keyboards.colored import btn_primary, btn_success, build_keyboard
from bot.responses import plain_error, mention
from bot.modules.start import send_newuser_log
from bot.timeutils import ist_clock, ist_date, ist_monday

logger = logging.getLogger(__name__)

# Scopes and their inclusive lower-bound dates (None = all time).
SCOPES: Tuple[str, ...] = ("overall", "today", "week")
_SCOPE_LABELS = {
    "overall": "Overall",
    "today": "Today",
    "week": "Weekly",
}
# Owner's custom emoji attached to each tab button (icon_custom_emoji_id).
# Button labels stay plain text — Telegram renders these IDs as the little
# custom-emoji icon in front of the label (same feature as the sticker
# buttons' EID.ADD).
_SCOPE_ICON = {
    "overall": EID.WEB,   # 🌐
    "today": EID.TIME,    # ⏰
    "week": EID.FIRE,     # 🔥
}

_TOP_LIMIT = 10
_TITLE_CLIP = 32


def _scope_since(scope: str) -> Optional[str]:
    """Inclusive lower-bound date for a scope (IST windows), None = all time.

    Today  → current IST day (refreshes at IST midnight).
    Weekly → Monday of the current IST week (refreshes Monday 00:00 IST).
    """
    if scope == "today":
        return ist_date()
    if scope == "week":
        return ist_monday()
    return None


def _is_milestone(total: int) -> bool:
    """100, then every 500 messages (500, 1000, 1500…)."""
    return total == 100 or (total >= 500 and total % 500 == 0)


def _rank_up_lines(chat_msgs: int, global_msgs: int,
                   mention_html: str) -> List[str]:
    """Announcement lines when totals cross a rank threshold.

    +1 chat rank per CHAT_RANK_MESSAGES (100) messages in the group,
    +1 global rank per GLOBAL_RANK_MESSAGES (250) messages overall —
    the same ladder /rank, /nextlevel and /profile display.
    """
    lines = []
    if chat_msgs > 0 and chat_msgs % CHAT_RANK_MESSAGES == 0:
        lines.append(
            f"{E.CROWN} {mention_html} reached "
            f"<b>Rank {db.chat_rank_for(chat_msgs)}</b> in this group!"
        )
    if global_msgs > 0 and global_msgs % GLOBAL_RANK_MESSAGES == 0:
        lines.append(
            f"{E.STAR} {mention_html} reached "
            f"<b>Global Rank {db.global_rank_for(global_msgs)}</b>!"
        )
    return lines


# ═════════════════════════════════════════════════════════════════
# Formatting helpers
# ═════════════════════════════════════════════════════════════════

def _fmt(n: int) -> str:
    """Thousands separators — 80,788."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _clip(text: str, limit: int = _TITLE_CLIP) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].strip() + "…"


def _mention_id(user_id: int, name: str) -> str:
    """Tagging-style profile mention: <a href="tg://user?id=..">Name</a>."""
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>'


def _name_of(row) -> str:
    """Display name from a users-JOIN row, with safe fallbacks."""
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full or (row.get("username") or "").strip() or f"User {row.get('user_id')}"


def _rank_line(index: int, mention_html: str, count: int) -> str:
    return f"{index}. {E.USER} {mention_html} • {_fmt(count)}"


# Board icons — owner's custom emoji set (E.* renders <tg-emoji>).
# E.CHART (📊) and raw 💬 are plain fallbacks, so use themed customs:
#   ⭐ Leaderboard / Top Groups headers, 📣 totals.
# Swap these two lines to re-theme the cards.
_ICON_BOARD = E.STAR
_ICON_TOTAL = E.ANNOUNCE


def _scope_markup(kind: str, scope: str, user_id: Optional[int] = None):
    """Active tab solo on row 1 (green + ✅), the rest blue below.

    Every tab uses the owner's custom emoji as its button icon."""
    def cb(s: str) -> str:
        return f"cs:{kind}:{s}" if kind == "r" else f"cs:m:{user_id}:{s}"

    active = btn_success(
        f"{_SCOPE_LABELS[scope]} \u2705", cb(scope),
        icon_emoji_id=_SCOPE_ICON[scope],
    )
    others = [
        btn_primary(_SCOPE_LABELS[s], cb(s), icon_emoji_id=_SCOPE_ICON[s])
        for s in SCOPES
        if s != scope
    ]
    return build_keyboard([[active], others])


# ═════════════════════════════════════════════════════════════════
# Board rendering (sync — small indexed queries, same as /daily)
# ═════════════════════════════════════════════════════════════════

def _rank_board(chat_id: int, scope: str) -> Tuple[str, Optional[object]]:
    since = _scope_since(scope)
    rows = db.get_chat_top(chat_id, since=since, limit=_TOP_LIMIT)
    total = db.get_chat_message_total(chat_id, since=since)

    lines = [f"{_ICON_BOARD} <b>Leaderboard</b> · <i>{_SCOPE_LABELS[scope]}</i>"]
    if rows:
        for i, row in enumerate(rows, start=1):
            lines.append(_rank_line(
                i,
                _mention_id(row["user_id"], _name_of(row)),
                row["total_messages"],
            ))
        lines.append(E.BULLET)
        lines.append(f"{_ICON_TOTAL} Total messages: {_fmt(total)}")
        markup = _scope_markup("r", scope)
    else:
        lines.append(f"{E.INFO} No messages counted yet — start chatting!")
        markup = None
    return "\n".join(lines), markup


def _mytop_board(user, scope: str) -> Tuple[str, Optional[object]]:
    since = _scope_since(scope)
    rows = db.get_user_top_groups(user.id, since=since, limit=_TOP_LIMIT)

    lines = [f"{E.USER} {mention(user)}"]
    lines.append(
        f"{_ICON_BOARD} <b>Top Groups</b> · <i>{_SCOPE_LABELS[scope]}</i>"
    )
    if rows:
        for i, row in enumerate(rows, start=1):
            title = _clip(row.get("chat_title") or f"Chat {row['chat_id']}")
            lines.append(
                f"{i}. {E.FOLDER} <b>{escape(title)}</b> • {_fmt(row['total_messages'])}"
            )
        markup = _scope_markup("m", scope, user.id)
    else:
        lines.append(f"{E.INFO} No messages found for this period.")
        markup = None
    return "\n".join(lines), markup


# ═════════════════════════════════════════════════════════════════
# Counter — every group text message, DB off the event loop
# ═════════════════════════════════════════════════════════════════

async def count_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type == "private" or user is None or user.is_bot:
        return

    chat_id, user_id = chat.id, user.id
    chat_title = getattr(chat, "title", None)
    today = ist_date()  # IST day — refreshes at IST midnight

    def _db_work() -> Tuple[Optional[int], bool, int, int]:
        """→ (day_total, is_brand_new_user, chat_msgs, global_msgs).

        ``None`` total = not counted (blocked or error).
        """
        is_new = False
        try:
            # First contact → register the sender (no /start required),
            # cache membership for this group, and (afterwards) post the
            # #Newuser log. Best-effort: a registration hiccup must never
            # stop the counting itself.
            was_new = db.get_user(user_id) is None
            try:
                registered = db.add_user(
                    user_id, user.username, user.first_name, user.last_name
                )
                db.cache_group_member(chat_id, user_id)
                is_new = was_new and registered
            except Exception as e:  # noqa: BLE001
                logger.warning("chatstats register failed: %s", e)
            # Spam-blocked messages aren't counted (see bot/modules/antispam.py).
            if db.is_spam_blocked(user_id):
                return None, is_new, 0, 0
            db.count_message(chat_id, user_id, today, chat_title)
            day_total = db.get_chat_day_total(chat_id, today)
            chat_msgs, global_msgs = db.get_user_message_totals(chat_id, user_id)
            return day_total, is_new, chat_msgs, global_msgs
        except Exception as e:  # noqa: BLE001 — counting must never break a handler
            logger.warning("chatstats count failed: %s", e)
            return None, is_new, 0, 0

    total, is_new, chat_msgs, global_msgs = await asyncio.get_running_loop().run_in_executor(
        None, _db_work
    )

    if is_new:
        # Fires once per user, ever — logs to the configured log channel.
        await send_newuser_log(context, user, chat_title)

    if total is None:
        return

    # Rank-ups first, then the day milestone — ONE combined reply so a
    # double threshold (e.g. 100 msgs) doesn't spam two messages.
    lines = _rank_up_lines(
        chat_msgs, global_msgs,
        _mention_id(user_id, user.first_name or "User"),
    )
    if _is_milestone(total):
        lines.append(f"{E.FIRE} {_fmt(total)} messages reached today! ({ist_clock()})")

    if lines:
        try:
            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.HTML
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("rank/milestone reply failed: %s", e)


# ═════════════════════════════════════════════════════════════════
# Commands
# ═════════════════════════════════════════════════════════════════

async def rankings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        await msg.reply_text(
            plain_error("This command only works in groups."),
            parse_mode=ParseMode.HTML,
        )
        return
    text, markup = _rank_board(chat.id, "overall")
    await msg.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup
    )


async def mytop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    text, markup = _mytop_board(user, "overall")
    await msg.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup
    )


# ═════════════════════════════════════════════════════════════════
# Tab switching — cs:r:<scope> / cs:m:<user_id>:<scope>
# ═════════════════════════════════════════════════════════════════

async def board_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith("cs:"):
        return

    parts = query.data.split(":")
    kind: Optional[str] = None
    scope: Optional[str] = None
    key: Optional[int] = None
    if len(parts) == 3 and parts[1] == "r":
        kind, scope = "r", parts[2]
    elif len(parts) == 4 and parts[1] == "m":
        try:
            key = int(parts[2])
        except ValueError:
            return
        kind, scope = "m", parts[3]
    if kind is None or scope not in _SCOPE_LABELS:
        return

    board_msg = query.message
    if board_msg is None:
        return

    if kind == "m":
        presser = query.from_user
        if presser is None or presser.id != key:
            try:
                await query.answer(
                    "Only the original user can switch this board.",
                    show_alert=True,
                )
            except Exception:  # noqa: BLE001
                pass
            return

    if kind == "r":
        chat = board_msg.chat
        if chat is None or getattr(chat, "type", None) == "private":
            return
        text, markup = _rank_board(chat.id, scope)
    else:
        text, markup = _mytop_board(query.from_user, scope)

    try:
        await query.answer()
    except Exception:  # noqa: BLE001 — stale taps are harmless
        pass
    try:
        # PTB: CallbackQuery.edit_message_text — Message has no such method
        # (it only exposes edit_text), so always edit through the query.
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e):
            logger.debug("board tab edit failed: %s", e)
    except Exception as e:  # noqa: BLE001 — never crash the callback
        logger.warning("board tab edit failed: %s", e)


# ═════════════════════════════════════════════════════════════════

def setup(app: Application) -> List[str]:
    # Own group (18): PTB runs max ONE handler per group — sharing
    # group 0 with antispam.flood_watch shadowed the counter for every
    # plain group message (nobody was registered or counted).
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~COMMAND) & filters.ChatType.GROUPS,
            count_message,
        ),
        group=18,
    )
    app.add_handler(CommandHandler("rankings", rankings_command))
    app.add_handler(CommandHandler("mytop", mytop_command))
    app.add_handler(CallbackQueryHandler(board_callback, pattern=r"^cs:[rm]:"))
    return ["/rankings", "/mytop", "count_message", "cs: tabs"]
