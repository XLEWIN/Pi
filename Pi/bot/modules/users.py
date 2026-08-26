"""Users module — User registration, tracking, and logging.

Tracks all users in groups and DMs, logs activity to SQLite.
"""

import logging
from datetime import datetime
from typing import Optional

from telegram import Update, ChatMember, User
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ChatMemberHandler,
    filters,
)
from telegram.constants import ParseMode

from bot.database import db
from bot.modules.start import send_log, format_user_log

logger = logging.getLogger(__name__)


def get_user_display(user: User) -> str:
    """Get display name for a user."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


async def register_user(user: User, context: ContextTypes.DEFAULT_TYPE,
                        chat_id: int = None, chat_title: str = None,
                        action: str = "joined"):
    """Register a user and log the activity."""
    if user.is_bot:
        return

    # Add user to database
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_bot=user.is_bot
    )

    # Add to group if provided
    if chat_id and chat_title:
        db.add_group(chat_id, chat_title)
        db.add_group_member(chat_id, user.id, "member")

    # Log activity
    db.update_user_activity(
        user_id=user.id,
        action=action,
        chat_id=chat_id,
        chat_title=chat_title
    )

    # Send log to channel
    log_message = format_user_log(user, action, chat_title)
    await send_log(context, log_message)

    logger.info(f"User {user.id} ({get_user_display(user)}) {action}")


async def register_group_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register all members when bot is added to a group."""
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or chat.first_name

    # Check if bot was added
    bot_added = any(member.id == context.bot.id for member in update.message.new_chat_members)

    if bot_added:
        # Bot was added to a group, register it
        bot_user = await context.bot.get_me()
        db.add_user(
            user_id=bot_user.id,
            username=bot_user.username,
            first_name=bot_user.first_name,
            is_bot=True
        )
        db.add_group(chat_id, chat_title)
        db.add_group_member(chat_id, bot_user.id, "bot")

        # Log bot addition
        log_message = f"🤖 <b>Bot added to chat</b>\n"
        log_message += f"💬 Chat: <b>{chat_title}</b>\n"
        log_message += f"🆔 Chat ID: <code>{chat_id}</code>"
        await send_log(context, log_message)

        logger.info(f"Bot added to group {chat_title} ({chat_id})")

        # Try to get group members
        try:
            member_count = await context.bot.get_chat_member_count(chat_id)

            log_message = f"📊 <b>Group stats</b>\n"
            log_message += f"💬 Chat: <b>{chat_title}</b>\n"
            log_message += f"👥 Members: {member_count}"
            await send_log(context, log_message)

        except Exception as e:
            logger.error(f"Error getting group info: {e}")

    # Register other new members
    for member in update.message.new_chat_members:
        if not member.is_bot:
            await register_user(
                member, context,
                chat_id=chat_id,
                chat_title=chat_title,
                action=f"joined {chat_title}"
            )


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new members joining a chat."""
    if not update.chat_member:
        return

    chat_member_update = update.chat_member
    new_member = chat_member_update.new_chat_member
    old_member = chat_member_update.old_chat_member
    user = chat_member_update.from_user

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or chat.first_name

    # Check if this is a new member joining
    if (new_member.status in [ChatMember.MEMBER, ChatMember.RESTRICTED] and
            old_member.status in [ChatMember.LEFT, ChatMember.BANNED]):

        await register_user(
            user, context,
            chat_id=chat_id,
            chat_title=chat_title,
            action=f"joined {chat_title}"
        )


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track messages to register active users."""
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat

    # Skip if it's a bot
    if user.is_bot:
        return

    # Register/update user
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_bot=user.is_bot
    )

    # Update activity (don't log every message to avoid spam)
    # Only update in database, don't send to log channel
    db.update_user_activity(
        user_id=user.id,
        action="sent message",
        chat_id=chat.id,
        chat_title=chat.title or chat.first_name
    )


async def userstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /userstats — show bot statistics."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    user_count = db.get_user_count()
    group_count = db.get_group_count()

    stats_text = f"""📊 Bot Statistics

👥 Total Users: {user_count}
💬 Total Groups: {group_count}

Use /myinfo to see your info.
Use /recentactivity to see recent activity."""

    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)


async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myinfo — show user's own info."""
    user = update.effective_user
    user_data = db.get_user(user.id)

    if not user_data:
        await update.message.reply_text("❌ You are not registered yet.")
        return

    username = f"@{user_data['username']}" if user_data['username'] else "No username"
    name = user_data['first_name'] or "Unknown"

    info_text = f"""👤 Your Information

🆔 User ID: <code>{user_data['user_id']}</code>
📛 Username: {username}
📛 Name: {name}
📅 First Seen: {user_data['first_seen']}
📅 Last Seen: {user_data['last_seen']}

📊 Status: {"🔴 Banned" if user_data['is_banned'] else "🟢 Active"}
🔇 Muted: {"Yes" if user_data['is_muted'] else "No"}
⚠️ Warnings: {user_data['warnings']}"""

    await update.message.reply_text(info_text, parse_mode=ParseMode.HTML)


async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /userinfo @user — show user info (admin only)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    # Check if user is admin
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text("❌ You need admin permissions to use this command.")
            return
    except Exception:
        await update.message.reply_text("❌ Error checking permissions.")
        return

    # Get target user
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args and context.args[0].startswith("@"):
        try:
            member = await context.bot.get_chat_member(chat_id, context.args[0])
            target_user = member.user
        except Exception:
            pass
    elif context.args:
        try:
            member = await context.bot.get_chat_member(chat_id, int(context.args[0]))
            target_user = member.user
        except Exception:
            pass

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• /userinfo @user\n"
            "• Reply to a message with /userinfo",
            parse_mode=ParseMode.HTML
        )
        return

    user_data = db.get_user(target_user.id)

    if not user_data:
        await update.message.reply_text("❌ User not found in database.")
        return

    username = f"@{user_data['username']}" if user_data['username'] else "No username"
    name = user_data['first_name'] or "Unknown"

    info_text = f"""👤 User Information

🆔 User ID: <code>{user_data['user_id']}</code>
📛 Username: {username}
📛 Name: {name}
📅 First Seen: {user_data['first_seen']}
📅 Last Seen: {user_data['last_seen']}

📊 Status: {"🔴 Banned" if user_data['is_banned'] else "🟢 Active"}
🔇 Muted: {"Yes" if user_data['is_muted'] else "No"}
⚠️ Warnings: {user_data['warnings']}"""

    await update.message.reply_text(info_text, parse_mode=ParseMode.HTML)


async def recentactivity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recentactivity — show recent activity."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    # Check if user is admin
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text("❌ You need admin permissions to use this command.")
            return
    except Exception:
        await update.message.reply_text("❌ Error checking permissions.")
        return

    activity = db.get_recent_activity(limit=5)

    if not activity:
        await update.message.reply_text("📭 No recent activity.")
        return

    activity_text = "📜 Recent Activity\n\n"

    for act in activity:
        username = f"@{act['username']}" if act.get('username') else "Unknown"
        activity_text += f"• {act['action']} by {username}\n"
        activity_text += f"  📅 {act['timestamp']}\n\n"

    await update.message.reply_text(activity_text, parse_mode=ParseMode.HTML)


# ============================================
# MODULE SETUP
# ============================================


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    handlers = []

    # Track new members joining
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, register_group_members))

    # Track chat member updates (for privacy mode)
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # Track all messages to register active users
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    # Commands
    app.add_handler(CommandHandler("userstats", userstats_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("userinfo", userinfo_command))
    app.add_handler(CommandHandler("recentactivity", recentactivity_command))

    handlers.extend([
        "new_chat_members tracker",
        "chat_member handler",
        "message tracker",
        "/userstats",
        "/myinfo",
        "/userinfo",
        "/recentactivity"
    ])

    return handlers
