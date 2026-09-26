"""Leveling module — ranks, rank cards, leaderboards, streaks.

Every number shown here derives from daily_messages + the rank ladders
in bot/constants.py (CHAT_RANK_MESSAGES / GLOBAL_RANK_MESSAGES) — the
same source /rankings displays. XP and streaks are cosmetic extras.
"""

import os
import logging
import tempfile
from html import escape

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from bot.command_handler import COMMAND, CommandHandler
from bot.constants import CHAT_RANK_MESSAGES, GLOBAL_RANK_MESSAGES
from bot.database import db
from bot.keyboards.colored import btn_primary, btn_url, build_keyboard
from bot.profile_templates import get_theme_list, THEMES
from bot.rank_image import create_rank_card
from bot.emojis import E, EID
from bot.timeutils import ist_monday, ist_month_start

logger = logging.getLogger(__name__)

# Cooldown tracking (user_id -> last_message_timestamp)
_cooldowns = {}


def _next_in_rank(messages: int, step: int) -> int:
    """Messages still needed to reach the next rank on a `step` ladder."""
    return step - (messages % step)


def _bar(done: int, total: int, width: int = 10) -> tuple[str, int]:
    """Text progress bar + percent for a `done/total` step."""
    pct = min(int(done * 100 / total), 100) if total else 0
    filled = min(int(done * width / total), width) if total else 0
    return "▰" * filled + "▱" * (width - filled), pct


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Bot username for deep links (same lookup as bot/modules/help.py)."""
    try:
        name = (context.bot_data or {}).get("username")
    except (AttributeError, TypeError):
        name = None
    name = name or getattr(context.bot, "username", None)
    return name or "PiModulerBot"


def _see_rank_keyboard(context: ContextTypes.DEFAULT_TYPE):
    """Colored 'See Your Rank' button — opens the bot DM and starts it."""
    url = f"https://t.me/{_bot_username(context)}?start=rank"
    return build_keyboard(
        [[btn_url("See Your Rank", url, icon_emoji_id=EID.CROWN, style="primary")]]
    )


def _nextlevel_keyboard():
    """Colored 'My Next Level' button — re-sends the progress card."""
    return build_keyboard(
        [[btn_primary("My Next Level", "nextlevel:me", icon_emoji_id=EID.FIRE)]]
    )


def _rank_caption(name: str) -> str:
    """Rank-card caption: header line only (details live on the card)."""
    return f"{E.CROWN} <b>Rank card for {escape(name)}</b>"


def _progress_text(info: dict, is_group: bool) -> str:
    """Unified next-level card — used by /nextlevel and its button."""
    chat_msgs = info["chat_messages"]
    global_msgs = info["global_messages"]
    cr, gr = info["chat_rank"], info["global_rank"]

    lines = [f"{E.CROWN} <b>Rank Progress</b>"]

    if is_group:
        done = chat_msgs % CHAT_RANK_MESSAGES
        bar, pct = _bar(done, CHAT_RANK_MESSAGES)
        lines.append(
            f"├ {E.LEVEL} Chat: <b>Rank {cr} → {cr + 1}</b> · "
            f"<b>{_next_in_rank(chat_msgs, CHAT_RANK_MESSAGES)}</b> messages to go"
        )
        lines.append(f"│  {done}/{CHAT_RANK_MESSAGES} {bar} ({pct}%)")

    done = global_msgs % GLOBAL_RANK_MESSAGES
    bar, pct = _bar(done, GLOBAL_RANK_MESSAGES)
    lines.append(
        f"├ {E.WEB} Global: <b>Rank {gr} → {gr + 1}</b> · "
        f"<b>{_next_in_rank(global_msgs, GLOBAL_RANK_MESSAGES)}</b> messages to go"
    )
    lines.append(f"│  {done}/{GLOBAL_RANK_MESSAGES} {bar} ({pct}%)")

    if is_group:
        lines.append(
            f"└ {E.INFO} Messages: {chat_msgs:,} in this group · {global_msgs:,} total"
        )
    else:
        lines.append(
            f"└ {E.INFO} Messages: {global_msgs:,} total · chat rank counts per group"
        )
    return "\n".join(lines)


# ── Message tracker for XP ──────────────────────────────
async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track messages for XP + streaks (DB work runs off the event loop).

    Message COUNTS and rank-up announcements belong to the counter in
    bot/modules/chatstats.py — this path only awards cosmetic XP.
    """
    if not update.message or update.effective_chat.type == "private":
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    # Spam-blocked users earn no XP either (bot/modules/antispam.py).
    if db.is_spam_blocked(user.id):
        return

    chat_id = update.effective_chat.id
    user_id = user.id

    # Cooldown: 30 seconds between XP gains
    import time
    now = time.time()
    cooldown_key = f"{user_id}:{chat_id}"
    if cooldown_key in _cooldowns and now - _cooldowns[cooldown_key] < 30:
        return
    _cooldowns[cooldown_key] = now

    def _db_work():
        try:
            db.add_message_xp(user_id, chat_id)
        except Exception as e:
            logger.warning(f"XP track failed: {e}")

    # SQLite is synchronous — run it off the event loop so commands stay fast.
    import asyncio
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_work)


# ── Command handlers ─────────────────────────────────────
async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rank — show user rank card using smash-style renderer."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id

    # Get target user
    if context.args and context.args[0].startswith("@"):
        try:
            member = await context.bot.get_chat_member(chat_id, context.args[0])
            target_user = member.user
        except Exception:
            await update.message.reply_text("User not found.")
            return
    elif update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user

    user_id = target_user.id

    # Unified rank info — daily_messages is the source of truth.
    info = db.get_user_rank_info(user_id, chat_id)

    name = target_user.first_name or "User"
    username = target_user.username or ""
    level = info["chat_rank"]
    chat_msgs = info["chat_messages"]
    global_msgs = info["global_messages"]
    template_id = info["template"]
    in_rank = chat_msgs % CHAT_RANK_MESSAGES
    progress_pct = min(int(in_rank * 100 / CHAT_RANK_MESSAGES), 100)
    position = info["chat_position"]
    total_members = info["chat_members"]

    # Download avatar
    avatar_path = None
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.photos:
            f = await context.bot.get_file(photos.photos[0][-1].file_id)
            avatar_path = os.path.join(tempfile.gettempdir(), f"avatar_{user_id}.jpg")
            await f.download_to_drive(avatar_path)
    except Exception as e:
        logger.warning(f"Avatar download failed: {e}")

    # Generate rank card using smash-style renderer
    output_path = os.path.join(tempfile.gettempdir(), f"rank_{user_id}.png")
    try:
        result = create_rank_card(
            name=name,
            username=f"@{username}" if username else "",
            level=level,
            next_level=level + 1,
            progress_pct=progress_pct,
            rank_text=f"#{position}/{total_members}" if position else f"—/{total_members}",
            messages=f"{chat_msgs:,}",
            global_messages=f"{global_msgs:,}",
            output_path=output_path,
            avatar_path=avatar_path,
            template_id=template_id,
        )

        if result and os.path.exists(output_path):
            caption = _rank_caption(name)
            with open(output_path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_see_rank_keyboard(context),
                )
        else:
            await update.message.reply_text("Error generating rank card.")
    except Exception as e:
        await update.message.reply_text(f"Error generating rank card: {e}")
    finally:
        # Cleanup temp files
        for path in (output_path, avatar_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


async def ranktemplate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ranktemplate — pick rank card template (DM only)."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(f"{E.INFO} Use this command in my DM for privacy.",
            parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text(
            f"{E.SETTINGS} <b>Rank Templates</b>\n\n"
            f"{get_theme_list()}\n\n"
            f"<b>Usage:</b> /ranktemplate &lt;number&gt;\n"
            f"<b>Example:</b> /ranktemplate 3",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        template = int(context.args[0])
        if template not in THEMES:
            await update.message.reply_text(f"{E.ERROR} Invalid template. Choose 1-6.",
            parse_mode=ParseMode.HTML)
            return
    except ValueError:
        await update.message.reply_text(f"{E.ERROR} Please provide a number (1-6).",
            parse_mode=ParseMode.HTML)
        return

    db.set_template(update.effective_user.id, template)
    theme_name = THEMES[template]["name"]
    await update.message.reply_text(f"{E.CHECK} Template set to {theme_name}!",
            parse_mode=ParseMode.HTML)


async def nextlevel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /nextlevel — messages needed for the next chat/global rank."""
    user_id = update.effective_user.id
    is_group = update.effective_chat.type != "private"
    info = db.get_user_rank_info(user_id, update.effective_chat.id if is_group else None)

    await update.message.reply_text(
        _progress_text(info, is_group),
        parse_mode=ParseMode.HTML,
        reply_markup=_nextlevel_keyboard(),
    )


async def nextlevel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 'My Next Level' button — fresh progress card, new message."""
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    await query.answer()

    chat = query.message.chat
    is_group = getattr(chat, "type", "private") != "private"
    info = db.get_user_rank_info(
        query.from_user.id, chat.id if is_group else None
    )
    await query.message.reply_text(
        _progress_text(info, is_group),
        parse_mode=ParseMode.HTML,
        reply_markup=_nextlevel_keyboard(),
    )


async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /streak — show message streak."""
    user_id = update.effective_user.id
    user_data = db.get_user_level(user_id)
    current = user_data.get("streak_current", 0)
    best = user_data.get("streak_best", 0)

    await update.message.reply_text(
        f"{E.FIRE} Message Streak\n"
        f"├ Current: <b>{current}</b> days\n"
        f"└ Best: <b>{best}</b> days",
        parse_mode=ParseMode.HTML,
    )


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /leaderboard /lb — show chat leaderboard."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    lb = db.get_leaderboard(chat_id, limit=10)

    if not lb:
        await update.message.reply_text(f"{E.INFO} No leaderboard data yet. Start chatting!",
            parse_mode=ParseMode.HTML)
        return

    medals = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3]
    lines = [f"{E.STAR} <b>Leaderboard — {update.effective_chat.title}</b>\n"]

    for i, entry in enumerate(lb):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(
            f"{medal} <b>{name}</b> — Rank {entry['rank']} · "
            f"{entry['messages']:,} msgs"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /daily — top chatters today."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_daily_top(chat_id, limit=10)

    if not top:
        await update.message.reply_text(f"{E.INFO} No messages today yet. Be the first!",
            parse_mode=ParseMode.HTML)
        return

    medals = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3]
    lines = [f"{E.TIME} <b>Today's Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {entry['messages']:,} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /weekly — top chatters this week (Monday IST, like /rankings)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_period_top(chat_id, since=ist_monday(), limit=10)

    if not top:
        await update.message.reply_text(f"{E.INFO} No messages this week yet!",
            parse_mode=ParseMode.HTML)
        return

    medals = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3]
    lines = [f"{E.TIME} <b>Weekly Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {entry.get('total_messages', 0):,} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /monthly — top chatters this month (IST calendar month)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_period_top(chat_id, since=ist_month_start(), limit=10)

    if not top:
        await update.message.reply_text(f"{E.INFO} No messages this month yet!",
            parse_mode=ParseMode.HTML)
        return

    medals = [E.MEDAL_1, E.MEDAL_2, E.MEDAL_3]
    lines = [f"{E.TIME} <b>Monthly Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {entry.get('total_messages', 0):,} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register leveling commands and message tracker."""
    # Commands
    app.add_handler(CommandHandler("rank", rank_command))
    app.add_handler(CommandHandler("ranktemplate", ranktemplate_command))
    app.add_handler(CommandHandler("nextlevel", nextlevel_command))
    app.add_handler(
        CallbackQueryHandler(nextlevel_callback, pattern=r"^nextlevel:me$")
    )
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("lb", leaderboard_command))
    app.add_handler(CommandHandler("daily", daily_command))
    app.add_handler(CommandHandler("weekly", weekly_command))
    app.add_handler(CommandHandler("monthly", monthly_command))

    # Message tracker for XP (group 5 to avoid conflicts)
    app.add_handler(
        MessageHandler(filters.TEXT & ~COMMAND, track_message),
        group=5,
    )

    return ["rank", "ranktemplate", "nextlevel", "streak", "leaderboard", "daily", "weekly", "monthly"]
