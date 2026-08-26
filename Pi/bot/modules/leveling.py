"""Leveling module — XP, levels, rank cards, leaderboards, streaks."""

import os
import logging
import tempfile
from datetime import date, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from bot.database import db
from bot.profile_templates import get_theme_list, THEMES
from bot.rank_image import create_rank_card

logger = logging.getLogger(__name__)

# Cooldown tracking (user_id -> last_message_timestamp)
_cooldowns = {}


def _get_xp_needed(level: int) -> int:
    """XP needed to reach next level (100 per level)."""
    return level * 100


def _format_number(n: int) -> str:
    """Format number with K/M suffix."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ── Message tracker for XP ──────────────────────────────
async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track messages for XP gain."""
    if not update.message or update.effective_chat.type == "private":
        return

    user = update.effective_user
    if not user or user.is_bot:
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

    # Add XP
    level_ups, new_level, leveled_up = db.add_message_xp(user_id, chat_id)

    # Announce level up (only every 5 levels to avoid spam)
    if leveled_up and new_level % 5 == 0:
        user_data = db.get_user_level(user_id)
        name = user.first_name or "User"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎉 <b>{name}</b> leveled up to <b>Level {new_level}</b>!",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ── Command handlers ─────────────────────────────────────
async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rank — show user rank card using smash-style renderer."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
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

    # Get data
    user_data = db.get_user_level(user_id)
    chat_data = db.get_chat_level(chat_id, user_id)
    rank = db.get_user_rank_in_chat(chat_id, user_id)
    total_members = db.get_total_chat_members(chat_id)

    name = target_user.first_name or "User"
    username = target_user.username or ""
    level = user_data.get("global_level", 1)
    global_msgs = user_data.get("global_messages", 0)
    chat_msgs = chat_data.get("messages", 0)
    xp = user_data.get("global_xp", 0)
    needed = _get_xp_needed(level)
    progress_pct = min(int((xp % needed) / needed * 100), 100) if needed > 0 else 0

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
            rank_text=f"#{rank}/{total_members}",
            messages=f"{chat_msgs:,}",
            global_messages=f"{global_msgs:,}",
            output_path=output_path,
            avatar_path=avatar_path,
        )

        if result and os.path.exists(output_path):
            with open(output_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption=f"📊 Rank card for {name}")
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
        await update.message.reply_text("Use this command in my DM for privacy.")
        return

    if not context.args:
        await update.message.reply_text(
            f"<b>Rank Templates</b>\n\n"
            f"{get_theme_list()}\n\n"
            f"<b>Usage:</b> /ranktemplate &lt;number&gt;\n"
            f"<b>Example:</b> /ranktemplate 3",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        template = int(context.args[0])
        if template not in THEMES:
            await update.message.reply_text("Invalid template. Choose 1-6.")
            return
    except ValueError:
        await update.message.reply_text("Please provide a number (1-6).")
        return

    db.set_template(update.effective_user.id, template)
    theme_name = THEMES[template]["name"]
    await update.message.reply_text(f"Template set to {theme_name}!")


async def nextlevel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /nextlevel — show XP needed for next level."""
    user_id = update.effective_user.id
    user_data = db.get_user_level(user_id)
    level = user_data.get("global_level", 1)
    xp = user_data.get("global_xp", 0)
    needed = _get_xp_needed(level)
    current_xp = xp % needed

    await update.message.reply_text(
        f"📊 <b>Level Progress</b>\n\n"
        f"  Level: <b>{level}</b>\n"
        f"  XP: <b>{current_xp}</b> / {needed}\n"
        f"  Total XP: <b>{xp}</b>\n"
        f"  Messages: <b>{user_data.get('global_messages', 0)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /streak — show message streak."""
    user_id = update.effective_user.id
    user_data = db.get_user_level(user_id)
    current = user_data.get("streak_current", 0)
    best = user_data.get("streak_best", 0)

    await update.message.reply_text(
        f"🔥 <b>Message Streak</b>\n\n"
        f"  Current: <b>{current}</b> days\n"
        f"  Best: <b>{best}</b> days",
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
        await update.message.reply_text("No leaderboard data yet. Start chatting!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>📊 Leaderboard — {update.effective_chat.title}</b>\n"]

    for i, entry in enumerate(lb):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — Level {entry['level']} ({_format_number(entry.get('xp', 0))} XP)")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /daily — top chatters today."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_daily_top(chat_id, limit=10)

    if not top:
        await update.message.reply_text("No messages today yet. Be the first!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>📅 Today's Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {entry['messages']} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /weekly — top chatters this week."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_period_top(chat_id, days=7, limit=10)

    if not top:
        await update.message.reply_text("No messages this week yet!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>📆 Weekly Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {_format_number(entry.get('total_messages', 0))} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def monthly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /monthly — top chatters this month."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    top = db.get_period_top(chat_id, days=30, limit=10)

    if not top:
        await update.message.reply_text("No messages this month yet!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>📆 Monthly Top Chatters</b>\n"]

    for i, entry in enumerate(top):
        name = entry.get("first_name") or entry.get("username") or str(entry["user_id"])
        medal = medals[i] if i < 3 else f"  {i+1}."
        lines.append(f"{medal} <b>{name}</b> — {_format_number(entry.get('total_messages', 0))} messages")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register leveling commands and message tracker."""
    # Commands
    app.add_handler(CommandHandler("rank", rank_command))
    app.add_handler(CommandHandler("ranktemplate", ranktemplate_command))
    app.add_handler(CommandHandler("nextlevel", nextlevel_command))
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("lb", leaderboard_command))
    app.add_handler(CommandHandler("daily", daily_command))
    app.add_handler(CommandHandler("weekly", weekly_command))
    app.add_handler(CommandHandler("monthly", monthly_command))

    # Message tracker for XP (group 5 to avoid conflicts)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, track_message),
        group=5,
    )

    return ["rank", "ranktemplate", "nextlevel", "streak", "leaderboard", "daily", "weekly", "monthly"]
