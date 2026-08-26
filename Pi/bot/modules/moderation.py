"""Moderation module — Mute, Ban, Kick, Warnings, Rules commands.

Works in groups only. Requires admin permissions.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum

from telegram import Update, ChatMember, ChatPermissions, User
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


# ============================================
# CONFIGURATION
# ============================================


class WarningAction(Enum):
    MUTE = "mute"
    KICK = "kick"
    BAN = "ban"
    TIMEOUT = "timeout"


# Store warnings and settings per chat
warnings_db: Dict[int, Dict[int, list]] = {}
settings_db: Dict[int, Dict[str, Any]] = {}
rules_db: Dict[int, Dict[str, Any]] = {}

DEFAULT_SETTINGS = {
    "warn_limit": 3,
    "warn_mode": WarningAction.MUTE,
    "warn_mode_duration": None,
    "warn_time": None,
    "private_rules": False,
}


# ============================================
# HELPER FUNCTIONS
# ============================================


def parse_duration(duration_str: str) -> Optional[timedelta]:
    """Parse duration string like '30s', '5m', '1h', '2d', '1w'."""
    if not duration_str:
        return None

    duration_str = duration_str.lower().strip()

    try:
        if duration_str.endswith("s"):
            return timedelta(seconds=int(duration_str[:-1]))
        elif duration_str.endswith("m"):
            return timedelta(minutes=int(duration_str[:-1]))
        elif duration_str.endswith("h"):
            return timedelta(hours=int(duration_str[:-1]))
        elif duration_str.endswith("d"):
            return timedelta(days=int(duration_str[:-1]))
        elif duration_str.endswith("w"):
            return timedelta(weeks=int(duration_str[:-1]))
    except (ValueError, IndexError):
        return None

    return None


def get_ordinal(n: int) -> str:
    """Get ordinal suffix for a number."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd'][n % 10] if n % 10 < 4 else 'th'}"


def get_user_display(user: User) -> str:
    """Get a display string for a user."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


async def get_target_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[User]:
    """
    Extract target user from:
    1. Reply to a message
    2. @username mention
    3. User ID
    4. Text mention entity
    """
    message = update.message
    if not message:
        return None

    # Method 1: Check if replying to a message
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    # Method 2: Check for text_mention entities (privacy mode mentions)
    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                return entity.user

    # Method 3: Parse from command arguments
    if context.args and len(context.args) > 0:
        target = context.args[0]

        # Try as @username
        if target.startswith("@"):
            try:
                member = await context.bot.get_chat_member(
                    update.effective_chat.id, target
                )
                if member and member.user:
                    return member.user
            except Exception:
                pass
            return None

        # Try as numeric user ID
        try:
            user_id = int(target)
            member = await context.bot.get_chat_member(
                update.effective_chat.id, user_id
            )
            if member and member.user:
                return member.user
        except (ValueError, Exception):
            pass

    return None


async def is_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None
) -> bool:
    """Check if a user is an admin in the chat."""
    if user_id is None:
        if update.effective_user:
            user_id = update.effective_user.id
        else:
            return False

    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False


async def is_bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the bot is an admin in the chat."""
    try:
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
        return bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Error checking bot admin status: {e}")
        return False


async def get_chat_settings(chat_id: int) -> Dict[str, Any]:
    """Get settings for a chat."""
    if chat_id not in settings_db:
        settings_db[chat_id] = DEFAULT_SETTINGS.copy()
    return settings_db[chat_id]


async def add_warning(chat_id: int, user_id: int, reason: str) -> int:
    """Add a warning to a user and return the warning count."""
    if chat_id not in warnings_db:
        warnings_db[chat_id] = {}
    if user_id not in warnings_db[chat_id]:
        warnings_db[chat_id][user_id] = []

    warnings_db[chat_id][user_id].append(
        {"reason": reason, "timestamp": datetime.now()}
    )

    return len(warnings_db[chat_id][user_id])


async def get_warnings(chat_id: int, user_id: int) -> list:
    """Get active warnings for a user."""
    if chat_id not in warnings_db:
        return []
    return warnings_db[chat_id].get(user_id, [])


async def remove_latest_warning(chat_id: int, user_id: int) -> bool:
    """Remove the latest warning for a user."""
    if chat_id in warnings_db and user_id in warnings_db[chat_id]:
        if warnings_db[chat_id][user_id]:
            warnings_db[chat_id][user_id].pop()
            return True
    return False


async def reset_warnings(chat_id: int, user_id: int) -> int:
    """Reset all warnings for a user and return count of cleared warnings."""
    if chat_id in warnings_db and user_id in warnings_db[chat_id]:
        count = len(warnings_db[chat_id][user_id])
        warnings_db[chat_id][user_id] = []
        return count
    return 0


async def reset_all_warnings(chat_id: int) -> int:
    """Reset all warnings in a chat and return count."""
    if chat_id in warnings_db:
        count = sum(len(warnings) for warnings in warnings_db[chat_id].values())
        warnings_db[chat_id] = {}
        return count
    return 0


async def execute_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: WarningAction,
    duration: Optional[timedelta] = None,
    reason: str = "",
):
    """Execute moderation action on a user."""
    chat_id = update.effective_chat.id

    try:
        if action == WarningAction.MUTE:
            until_date = datetime.now() + duration if duration else None
            permissions = ChatPermissions(can_send_messages=False)
            await context.bot.restrict_chat_member(
                chat_id, user_id, permissions, until_date=until_date
            )
            if duration:
                return f"🔇 Muted <a href='tg://user?id={user_id}'>{user_id}</a> for {duration}."
            else:
                return f"🔇 Muted <a href='tg://user?id={user_id}'>{user_id}</a> permanently."

        elif action == WarningAction.KICK:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
            return f"👢 Kicked <a href='tg://user?id={user_id}'>{user_id}</a>."

        elif action == WarningAction.BAN:
            until_date = datetime.now() + duration if duration else None
            await context.bot.ban_chat_member(chat_id, user_id, until_date=until_date)
            if duration:
                return f"🔨 Banned <a href='tg://user?id={user_id}'>{user_id}</a> for {duration}."
            else:
                return f"🔨 Banned <a href='tg://user?id={user_id}'>{user_id}</a> permanently."

        elif action == WarningAction.TIMEOUT:
            if not duration:
                duration = timedelta(hours=1)
            until_date = datetime.now() + duration
            permissions = ChatPermissions(can_send_messages=False)
            await context.bot.restrict_chat_member(
                chat_id, user_id, permissions, until_date=until_date
            )
            return f"⏳ Timed out <a href='tg://user?id={user_id}'>{user_id}</a> for {duration}."

    except Exception as e:
        logger.error(f"Error executing action: {e}")
        return f"❌ Failed to execute action: {str(e)}"

    return ""


# ============================================
# MUTE COMMANDS
# ============================================


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /mute command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to mute users.\n"
            "Required permission: <b>Can Restrict Members</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to mute users.\n"
            "Please make sure I have the <b>Can Restrict Members</b> permission.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to mute.\n\n"
            "<b>Usage:</b>\n"
            "• /mute @user [period] [reason]\n"
            "• Reply to a message with /mute [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot mute yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot mute myself.")
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        remaining_args = context.args[args_start:]

        if remaining_args:
            parsed_duration = parse_duration(remaining_args[0])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(remaining_args[1:]) or reason
            else:
                reason = " ".join(remaining_args) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.MUTE, duration, reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def dmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dmute command - mute and delete message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to mute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to mute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        try:
            await update.message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dmute [period] [reason]\n"
            "• /dmute @user [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot mute yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot mute myself.")
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 0
        if context.args and not context.args[0].startswith("@"):
            parsed_duration = parse_duration(context.args[0])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[1:]) or reason
            else:
                reason = " ".join(context.args) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.MUTE, duration, reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    if message_deleted:
        reply_text += "\n🗑️ Deleted the offending message."
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def smute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /smute command - silent mute."""
    if update.effective_chat.type == "private":
        return

    if not await is_admin(update, context):
        return

    if not await is_bot_admin(update, context):
        return

    target_user = await get_target_user(update, context)

    if not target_user or target_user.id == update.effective_user.id:
        return

    if target_user.id == context.bot.id:
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            parsed_duration = parse_duration(context.args[args_start])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[args_start + 1 :]) or reason
            else:
                reason = " ".join(context.args[args_start:]) or reason

    await execute_action(
        update, context, target_user.id, WarningAction.MUTE, duration, reason
    )

    try:
        await update.message.delete()
    except Exception:
        pass


async def tmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tmute command - temporary mute."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to mute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to mute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to mute.\n\n"
            "<b>Usage:</b> /tmute @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tmute @user 1h Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot mute yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot mute myself.")
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            parsed_duration = parse_duration(context.args[args_start])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[args_start + 1 :]) or reason
            else:
                await update.message.reply_text(
                    "❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
                )
                return

    if not duration:
        await update.message.reply_text(
            "❌ Duration is required for temporary mute.\n\n"
            "<b>Usage:</b> /tmute @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tmute @user 1h Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    result = await execute_action(
        update, context, target_user.id, WarningAction.MUTE, duration, reason
    )

    unmute_time = datetime.now() + duration
    reply_text = (
        f"{result}\n<b>Reason:</b> {reason}\n"
        f"<b>Auto-unmute:</b> {unmute_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unmute command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to unmute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to unmute users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to unmute.\n\n"
            "<b>Usage:</b> /unmute @username or user ID",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        permissions = ChatPermissions(
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
            can_invite_users=True,
            can_change_info=True,
            can_pin_messages=True,
            can_manage_topics=True,
        )
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target_user.id, permissions
        )
        await update.message.reply_text(
            f"🔊 Unmuted <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to unmute user: {str(e)}")


# ============================================
# BAN COMMANDS
# ============================================


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ban command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to ban users.\n"
            "Required permission: <b>Can Ban Members</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to ban users.\n"
            "Please make sure I have the <b>Can Ban Members</b> permission.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to ban.\n\n"
            "<b>Usage:</b>\n"
            "• /ban @user [period] [reason]\n"
            "• Reply to a message with /ban [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot ban yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot ban myself.")
        return

    try:
        target_member = await context.bot.get_chat_member(
            update.effective_chat.id, target_user.id
        )
        if target_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            sender_member = await context.bot.get_chat_member(
                update.effective_chat.id, update.effective_user.id
            )
            if sender_member.status != ChatMember.OWNER:
                await update.message.reply_text(
                    "❌ You cannot ban a user with equal or higher permissions."
                )
                return
    except Exception:
        pass

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            parsed_duration = parse_duration(context.args[args_start])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[args_start + 1 :]) or reason
            else:
                reason = " ".join(context.args[args_start:]) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.BAN, duration, reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def dban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dban command - ban and delete message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to ban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to ban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        try:
            await update.message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dban [period] [reason]\n"
            "• /dban @user [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot ban yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot ban myself.")
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 0
        if context.args and not context.args[0].startswith("@"):
            parsed_duration = parse_duration(context.args[0])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[1:]) or reason
            else:
                reason = " ".join(context.args) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.BAN, duration, reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    if message_deleted:
        reply_text += "\n🗑️ Deleted the offending message."
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def sban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sban command - silent ban."""
    if update.effective_chat.type == "private":
        return

    if not await is_admin(update, context):
        return

    if not await is_bot_admin(update, context):
        return

    target_user = await get_target_user(update, context)

    if not target_user or target_user.id == update.effective_user.id:
        return

    if target_user.id == context.bot.id:
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            parsed_duration = parse_duration(context.args[args_start])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[args_start + 1 :]) or reason
            else:
                reason = " ".join(context.args[args_start:]) or reason

    await execute_action(
        update, context, target_user.id, WarningAction.BAN, duration, reason
    )

    try:
        await update.message.delete()
    except Exception:
        pass


async def tban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tban command - temporary ban."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to ban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to ban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to ban.\n\n"
            "<b>Usage:</b> /tban @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tban @user 7d Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot ban yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot ban myself.")
        return

    duration = None
    reason = "No reason provided"

    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            parsed_duration = parse_duration(context.args[args_start])
            if parsed_duration:
                duration = parsed_duration
                reason = " ".join(context.args[args_start + 1 :]) or reason
            else:
                await update.message.reply_text(
                    "❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
                )
                return

    if not duration:
        await update.message.reply_text(
            "❌ Duration is required for temporary ban.\n\n"
            "<b>Usage:</b> /tban @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tban @user 7d Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    result = await execute_action(
        update, context, target_user.id, WarningAction.BAN, duration, reason
    )

    unban_time = datetime.now() + duration
    reply_text = (
        f"{result}\n<b>Reason:</b> {reason}\n"
        f"<b>Auto-unban:</b> {unban_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unban command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to unban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to unban users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to unban.\n\n"
            "<b>Usage:</b> /unban @user or user ID",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target_user.id)
        await update.message.reply_text(
            f"✅ Unbanned <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to unban user: {str(e)}")


# ============================================
# KICK COMMANDS
# ============================================


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /kick command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to kick users.\n"
            "Required permission: <b>Can Ban Members</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to kick users.\n"
            "Please make sure I have the <b>Can Ban Members</b> permission.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to kick.\n\n"
            "<b>Usage:</b>\n"
            "• /kick @user [reason]\n"
            "• Reply to a message with /kick [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot kick yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot kick myself.")
        return

    try:
        target_member = await context.bot.get_chat_member(
            update.effective_chat.id, target_user.id
        )
        if target_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            sender_member = await context.bot.get_chat_member(
                update.effective_chat.id, update.effective_user.id
            )
            if sender_member.status != ChatMember.OWNER:
                await update.message.reply_text(
                    "❌ You cannot kick a user with equal or higher permissions."
                )
                return
    except Exception:
        pass

    reason = "No reason provided"
    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            reason = " ".join(context.args[args_start:]) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.KICK, reason=reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def dkick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dkick command - kick and delete message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to kick users.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await is_bot_admin(update, context):
        await update.message.reply_text(
            "❌ I don't have permission to kick users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        try:
            await update.message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dkick [reason]\n"
            "• /dkick @user [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot kick yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot kick myself.")
        return

    reason = "No reason provided"
    if context.args:
        reason = " ".join(context.args) or reason

    result = await execute_action(
        update, context, target_user.id, WarningAction.KICK, reason=reason
    )

    reply_text = f"{result}\n<b>Reason:</b> {reason}"
    if message_deleted:
        reply_text += "\n🗑️ Deleted the offending message."
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def skick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /skick command - silent kick."""
    if update.effective_chat.type == "private":
        return

    if not await is_admin(update, context):
        return

    if not await is_bot_admin(update, context):
        return

    target_user = await get_target_user(update, context)

    if not target_user or target_user.id == update.effective_user.id:
        return

    if target_user.id == context.bot.id:
        return

    reason = "No reason provided"
    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            reason = " ".join(context.args[args_start:]) or reason

    await execute_action(
        update, context, target_user.id, WarningAction.KICK, reason=reason
    )

    try:
        await update.message.delete()
    except Exception:
        pass


# ============================================
# WARNING COMMANDS
# ============================================


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warn command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to warn users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user to warn.\n\n"
            "<b>Usage:</b>\n"
            "• /warn @user [reason]\n"
            "• Reply to a message with /warn [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot warn yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot warn myself.")
        return

    reason = "No reason provided"
    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            reason = " ".join(context.args[args_start:]) or reason

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")

        result = await execute_action(
            update, context, target_user.id, action, duration, reason
        )

        reply_text = (
            f"⚠️ Warning issued to <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"({warning_count}/{settings['warn_limit']}).\n"
            f"<b>Reason:</b> {reason}\n\n"
            f"🚨 Action triggered: {result}"
        )

        await reset_warnings(chat_id, target_user.id)
    else:
        reply_text = (
            f"⚠️ Warning issued to <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"({warning_count}/{settings['warn_limit']}).\n"
            f"<b>Reason:</b> {reason}"
        )

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def dwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dwarn command - warn and delete message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to warn users.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        try:
            await update.message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dwarn [reason]\n"
            "• /dwarn @user [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == update.effective_user.id:
        await update.message.reply_text("❌ You cannot warn yourself.")
        return

    if target_user.id == context.bot.id:
        await update.message.reply_text("❌ I cannot warn myself.")
        return

    reason = "No reason provided"
    if context.args:
        reason = " ".join(context.args) or reason

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")

        result = await execute_action(
            update, context, target_user.id, action, duration, reason
        )

        reply_text = (
            f"⚠️ Warning issued to <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"({warning_count}/{settings['warn_limit']}).\n"
            f"<b>Reason:</b> {reason}"
        )
        if message_deleted:
            reply_text += "\n🗑️ Deleted the offending message."
        reply_text += f"\n\n🚨 Action triggered: {result}"

        await reset_warnings(chat_id, target_user.id)
    else:
        reply_text = (
            f"⚠️ Warning issued to <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"({warning_count}/{settings['warn_limit']}).\n"
            f"<b>Reason:</b> {reason}"
        )
        if message_deleted:
            reply_text += "\n🗑️ Deleted the offending message."

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def swarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /swarn command - silent warn."""
    if update.effective_chat.type == "private":
        return

    if not await is_admin(update, context):
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        return

    if target_user.id == update.effective_user.id:
        return

    if target_user.id == context.bot.id:
        return

    reason = "No reason provided"
    if context.args:
        args_start = 1 if context.args[0].startswith("@") else 0
        if len(context.args) > args_start:
            reason = " ".join(context.args[args_start:]) or reason

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")
        await execute_action(update, context, target_user.id, action, duration, reason)
        await reset_warnings(chat_id, target_user.id)

    try:
        await update.message.delete()
    except Exception:
        pass


async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warns command - show user warnings."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    target_user = await get_target_user(update, context)
    if not target_user:
        target_user = update.effective_user

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)
    warnings = await get_warnings(chat_id, target_user.id)

    if settings["warn_time"]:
        active_warnings = []
        for w in warnings:
            if datetime.now() - w["timestamp"] < settings["warn_time"]:
                active_warnings.append(w)
        warnings = active_warnings

    if warnings:
        warning_list = "\n".join(
            [
                f"• {w['reason']} ({w['timestamp'].strftime('%Y-%m-%d %H:%M')})"
                for w in warnings
            ]
        )
        reply_text = (
            f"⚠️ Active warnings for <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a>:\n"
            f"{warning_list}\n\n"
            f"<b>Total:</b> {len(warnings)}/{settings['warn_limit']}"
        )
    else:
        if target_user.id == update.effective_user.id:
            reply_text = "✅ You have no active warnings."
        else:
            reply_text = (
                f"✅ <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
                f"has no active warnings."
            )

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def rmwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rmwarn command - remove latest warning."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to manage warnings.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user.\n\n"
            "<b>Usage:</b> /rmwarn @user",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)

    success = await remove_latest_warning(chat_id, target_user.id)

    if success:
        warnings = await get_warnings(chat_id, target_user.id)
        reply_text = (
            f"✅ Removed the latest warning for "
            f"<a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a>.\n"
            f"<b>Warnings remaining:</b> {len(warnings)}/{settings['warn_limit']}"
        )
    else:
        reply_text = (
            f"⚠️ <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"has no active warnings to remove."
        )

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def resetwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetwarn command - clear all warnings for user."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to manage warnings.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(update, context)

    if not target_user:
        await update.message.reply_text(
            "❌ Please specify a user.\n\n"
            "<b>Usage:</b> /resetwarn @user",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)

    count = await reset_warnings(chat_id, target_user.id)

    if count > 0:
        reply_text = (
            f"✅ Cleared all {count} warnings for "
            f"<a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a>.\n"
            f"<b>Warnings reset to 0/{settings['warn_limit']}.</b>"
        )
    else:
        reply_text = (
            f"⚠️ <a href='tg://user?id={target_user.id}'>{get_user_display(target_user)}</a> "
            f"has no active warnings to clear."
        )

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def resetallwarns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetallwarns command - clear all warnings in chat."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to reset all warnings.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    count = await reset_all_warnings(chat_id)

    if count > 0:
        reply_text = f"✅ Cleared all {count} active warnings in this chat."
    else:
        reply_text = "✅ No active warnings found in this chat."

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


# ============================================
# WARNING CONFIGURATION COMMANDS
# ============================================


async def warnlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warnlimit command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to change warning settings.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        chat_id = update.effective_chat.id
        settings = await get_chat_settings(chat_id)
        await update.message.reply_text(
            f"⚙️ Current warning limit: <b>{settings['warn_limit']}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        limit = int(context.args[0])
        if limit < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Please provide a valid number.\n\n"
            "<b>Usage:</b> /warnlimit &lt;number&gt;\n"
            "<b>Example:</b> /warnlimit 3",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)
    settings["warn_limit"] = limit
    settings_db[chat_id] = settings

    await update.message.reply_text(
        f"⚙️ Warning limit set to <b>{limit}</b>.\n"
        f"Action will trigger on the {get_ordinal(limit)} warning.",
        parse_mode=ParseMode.HTML,
    )


async def warnmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warnmode command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to change warning settings.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        chat_id = update.effective_chat.id
        settings = await get_chat_settings(chat_id)
        await update.message.reply_text(
            f"⚙️ Current warning mode: <b>{settings['warn_mode'].value}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    mode_str = context.args[0].lower()
    try:
        mode = WarningAction(mode_str)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid warning mode. Choose from: <b>mute</b>, <b>kick</b>, <b>ban</b>, <b>timeout</b>\n\n"
            "<b>Usage:</b> /warnmode &lt;action&gt; [duration]\n"
            "<b>Example:</b> /warnmode mute 1d",
            parse_mode=ParseMode.HTML,
        )
        return

    duration = None
    if len(context.args) > 1:
        duration = parse_duration(context.args[1])
        if not duration:
            await update.message.reply_text(
                "❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
            )
            return

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)
    settings["warn_mode"] = mode
    settings["warn_mode_duration"] = duration
    settings_db[chat_id] = settings

    mode_descriptions = {
        WarningAction.MUTE: "Temporarily mute the user",
        WarningAction.KICK: "Remove the user from the group",
        WarningAction.BAN: "Permanently ban the user",
        WarningAction.TIMEOUT: "Restrict the user temporarily",
    }

    duration_text = f" for {duration}" if duration else ""
    reply_text = (
        f"⚙️ Warning mode set to: <b>{mode.value}</b>{duration_text}\n"
        f"{mode_descriptions[mode]}"
    )

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def warntime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /warntime command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to change warning settings.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args:
        chat_id = update.effective_chat.id
        settings = await get_chat_settings(chat_id)
        if settings["warn_time"]:
            await update.message.reply_text(
                f"⚙️ Warning expiration: <b>{settings['warn_time']}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "⚙️ Warning expiration: <b>Disabled</b> (warnings stay forever)",
                parse_mode=ParseMode.HTML,
            )
        return

    if context.args[0].lower() == "off":
        chat_id = update.effective_chat.id
        settings = await get_chat_settings(chat_id)
        settings["warn_time"] = None
        settings_db[chat_id] = settings

        await update.message.reply_text(
            "⚙️ Warning expiration disabled.\n"
            "Warnings will stay forever until cleared."
        )
        return

    duration = parse_duration(context.args[0])
    if not duration:
        await update.message.reply_text(
            "❌ Invalid duration format. Use: 30s, 5m, 1h, 2d, 1w, or <b>off</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)
    settings["warn_time"] = duration
    settings_db[chat_id] = settings

    await update.message.reply_text(
        f"⚙️ Warning expiration set to: <b>{duration}</b>\n"
        f"Warnings older than {duration} will stop counting.",
        parse_mode=ParseMode.HTML,
    )


# ============================================
# RULES COMMANDS
# ============================================


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /rules command."""
    chat_id = update.effective_chat.id

    if chat_id not in rules_db or not rules_db[chat_id].get("text"):
        await update.message.reply_text(
            "📜 No rules have been set for this chat yet.\n"
            "Admins can use /setrules to configure them."
        )
        return

    settings = await get_chat_settings(chat_id)
    rules = rules_db[chat_id]

    if settings.get("private_rules"):
        await update.message.reply_text(
            "📜 Rules for this chat\n\n"
            "Click the button below to view the rules in a private message.",
        )
        return

    reply_text = f"📜 Rules\n\n{rules['text']}"
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def setrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setrules command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to set rules.",
            parse_mode=ParseMode.HTML,
        )
        return

    if update.message.reply_to_message:
        replied_text = update.message.reply_to_message.text
        if replied_text:
            chat_id = update.effective_chat.id
            rules_db[chat_id] = {"text": replied_text}
            await update.message.reply_text(
                f"✅ Rules copied from the replied message!\n\n"
                f"<b>Preview:</b>\n{replied_text[:500]}{'...' if len(replied_text) > 500 else ''}"
            )
            return

    if not context.args:
        await update.message.reply_text(
            "❌ Please provide the rules text.\n\n"
            "<b>Usage:</b> /setrules &lt;text&gt;\n\n"
            "Or reply to a message with /setrules to copy its content.",
            parse_mode=ParseMode.HTML,
        )
        return

    rules_text = " ".join(context.args)
    chat_id = update.effective_chat.id
    rules_db[chat_id] = {"text": rules_text}

    await update.message.reply_text(
        f"✅ Rules updated successfully!\n\n"
        f"<b>Preview:</b>\n{rules_text[:500]}{'...' if len(rules_text) > 500 else ''}"
    )


async def resetrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetrules command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to reset rules.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id

    if chat_id in rules_db and rules_db[chat_id].get("text"):
        del rules_db[chat_id]
        await update.message.reply_text("✅ Rules have been cleared for this chat.")
    else:
        await update.message.reply_text("⚠️ No rules are currently set for this chat.")


async def privaterules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /privaterules command."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command can only be used in groups.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text(
            "❌ You don't have permission to change this setting.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.args or context.args[0].lower() not in ["on", "off"]:
        await update.message.reply_text(
            '❌ Please specify "on" or "off".\n\n'
            "<b>Usage:</b> /privaterules &lt;on|off&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = context.args[0].lower() == "on"
    chat_id = update.effective_chat.id
    settings = await get_chat_settings(chat_id)
    settings["private_rules"] = enabled
    settings_db[chat_id] = settings

    if enabled:
        await update.message.reply_text(
            "⚙️ Private rules enabled.\n"
            "/rules will now send a button that DMs the rules instead of replying inline."
        )
    else:
        await update.message.reply_text(
            "⚙️ Private rules disabled.\n"
            "/rules will now reply with the rules inline."
        )


# ============================================
# MODULE SETUP
# ============================================


def setup(app: Application) -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    handlers = []

    # Mute commands
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("dmute", dmute_command))
    app.add_handler(CommandHandler("smute", smute_command))
    app.add_handler(CommandHandler("tmute", tmute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    handlers.extend(["/mute", "/dmute", "/smute", "/tmute", "/unmute"])

    # Ban commands
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("dban", dban_command))
    app.add_handler(CommandHandler("sban", sban_command))
    app.add_handler(CommandHandler("tban", tban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    handlers.extend(["/ban", "/dban", "/sban", "/tban", "/unban"])

    # Kick commands
    app.add_handler(CommandHandler("kick", kick_command))
    app.add_handler(CommandHandler("dkick", dkick_command))
    app.add_handler(CommandHandler("skick", skick_command))
    handlers.extend(["/kick", "/dkick", "/skick"])

    # Warning commands
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("dwarn", dwarn_command))
    app.add_handler(CommandHandler("swarn", swarn_command))
    app.add_handler(CommandHandler("warns", warns_command))
    app.add_handler(CommandHandler("rmwarn", rmwarn_command))
    app.add_handler(CommandHandler("resetwarn", resetwarn_command))
    app.add_handler(CommandHandler("resetallwarns", resetallwarns_command))
    handlers.extend(["/warn", "/dwarn", "/swarn", "/warns", "/rmwarn", "/resetwarn", "/resetallwarns"])

    # Warning configuration
    app.add_handler(CommandHandler("warnlimit", warnlimit_command))
    app.add_handler(CommandHandler("warnmode", warnmode_command))
    app.add_handler(CommandHandler("warntime", warntime_command))
    handlers.extend(["/warnlimit", "/warnmode", "/warntime"])

    # Rules commands
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("setrules", setrules_command))
    app.add_handler(CommandHandler("resetrules", resetrules_command))
    app.add_handler(CommandHandler("privaterules", privaterules_command))
    handlers.extend(["/rules", "/setrules", "/resetrules", "/privaterules"])

    return handlers
