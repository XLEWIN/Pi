"""Moderation module — Mute, Ban, Kick, Warnings, Rules commands.

Works in groups only. Requires admin permissions.
Success replies use bot.responses action cards (Pi emoji set).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from enum import Enum
from html import escape

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.types import ChatPermissions, Message, User

from bot.emojis import E
from bot.pipeline import cmd, on
from bot.reply import reply_text
from bot.responses import (
    action_card,
    bot_rights_error,
    failed,
    field_by,
    field_count,
    field_duration,
    field_extra,
    field_reason,
    field_user,
    mention,
    plain_error,
    reply_card,
    user_label,
)

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
    message: Message, bot: Bot, args: list
) -> Optional[User]:
    """
    Extract target user from:
    1. Reply to a message
    2. @username mention
    3. User ID
    4. Text mention entity
    """
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
    if args and len(args) > 0:
        target = args[0]

        # Try as @username
        if target.startswith("@"):
            try:
                member = await bot.get_chat_member(
                    message.chat.id, target
                )
                if member and member.user:
                    return member.user
            except Exception:
                pass
            return None

        # Try as numeric user ID
        try:
            user_id = int(target)
            member = await bot.get_chat_member(
                message.chat.id, user_id
            )
            if member and member.user:
                return member.user
        except (ValueError, Exception):
            pass

    return None


#: Fallback reason shown on action cards when none was given.
DEFAULT_REASON = "No reason provided"


def action_args(message: Message, args: list) -> list[str]:
    """Command args remaining after the target — the (duration/)reason tokens.

    Mirrors :func:`get_target_user`'s resolution order so the target
    token is never mistaken for a reason (a numeric user ID must not
    leak into the "Reason" field of the action card):

    1. Reply target → every arg belongs to (duration/)reason.
    2. Text-mention entity target → whatever follows the mentioned
       text (entity offsets are UTF-16 code units).
    3. @username / numeric-ID target at ``args[0]`` → drop it.
    """
    args = list(args or [])
    if not args:
        return []
    if (
        message is not None
        and message.reply_to_message
        and message.reply_to_message.from_user
    ):
        return args
    if message is not None and message.entities:
        for entity in message.entities:
            if (
                getattr(entity, "type", None) == "text_mention"
                and getattr(entity, "user", None)
            ):
                text = message.text or message.caption or ""
                try:
                    encoded = text.encode("utf-16-le")
                    rest = encoded[
                        (entity.offset + entity.length) * 2 :
                    ].decode("utf-16-le", errors="ignore")
                except Exception:
                    rest = ""
                return rest.split()
    return args[1:]


def parse_duration_reason(
    args: Optional[list[str]],
) -> tuple[Optional[timedelta], str]:
    """Split remaining args into ``(duration, reason)`` with safe defaults.

    A missing/empty reason becomes :data:`DEFAULT_REASON`; a first token
    that is not a duration (``30s/5m/1h/2d/1w``) is part of the reason.
    """
    if not args:
        return None, DEFAULT_REASON
    duration = parse_duration(args[0])
    if duration:
        return duration, " ".join(args[1:]).strip() or DEFAULT_REASON
    return None, " ".join(args).strip() or DEFAULT_REASON


async def is_admin(
    message: Message, bot: Bot, user_id: int = None
) -> bool:
    """Check if a user is an admin in the chat."""
    if user_id is None:
        if message.from_user:
            user_id = message.from_user.id
        else:
            return False

    chat_id = message.chat.id

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception as e:
        logger.warning(f"Error checking admin status: {e}")
        return False


async def is_bot_admin(message: Message, bot: Bot) -> bool:
    """Check if the bot is an admin in the chat."""
    try:
        bot_member = await bot.get_chat_member(
            message.chat.id, bot.id
        )
        return bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception as e:
        logger.warning(f"Error checking bot admin status: {e}")
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


def _record_moderation_action(chat_id: int, user_id: int, kind: str) -> None:
    """Blocking Mongo counter writes (mod-actions + reputation event).

    MUST be called as ``await asyncio.to_thread(_record_moderation_action, ...)`` —
    ``bot.database`` is synchronous and must never run on the event loop.
    """
    from bot.database import db as _pdb
    _pdb.bump_mod_actions(chat_id, 1)
    _pdb.record_reputation_event(user_id, kind, 1)


def _record_reputation(user_id: int, kind: str, amount: int) -> None:
    """Blocking Mongo reputation write — MUST run inside ``asyncio.to_thread``."""
    from bot.database import db as _pdb
    _pdb.record_reputation_event(user_id, kind, amount)


async def execute_action(
    message: Message,
    bot: Bot,
    user_id: int,
    action: WarningAction,
    duration: Optional[timedelta] = None,
    reason: str = "",
):
    """Execute moderation action on a user. Returns (ok: bool, detail: str)."""
    chat_id = message.chat.id

    try:
        if action == WarningAction.MUTE:
            until_date = datetime.now() + duration if duration else None
            permissions = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(
                chat_id, user_id, permissions, until_date=until_date
            )
            if duration:
                return True, f"Muted for {duration}."
            return True, "Muted permanently."

        elif action == WarningAction.KICK:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            return True, "Kicked from the group."

        elif action == WarningAction.BAN:
            until_date = datetime.now() + duration if duration else None
            await bot.ban_chat_member(chat_id, user_id, until_date=until_date)
            if duration:
                return True, f"Banned for {duration}."
            return True, "Banned permanently."

        elif action == WarningAction.TIMEOUT:
            if not duration:
                duration = timedelta(hours=1)
            until_date = datetime.now() + duration
            permissions = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(
                chat_id, user_id, permissions, until_date=until_date
            )
            return True, f"Timed out for {duration}."

    except Exception as e:
        logger.warning(f"Error executing action: {e}")
        return False, str(e)

    return False, "Unknown action."


# Action result → card title + header icon
_ACTION_META = {
    WarningAction.MUTE: ("Mute Successful", E.MUTE),
    WarningAction.KICK: ("Kick Successful", E.KICK),
    WarningAction.BAN: ("Ban Successful", E.BAN),
    WarningAction.TIMEOUT: ("Timeout Successful", E.TIME),
}


def moderation_card(
    action: WarningAction,
    target: User,
    actor: Optional[User],
    reason: str,
    *,
    duration: Optional[timedelta] = None,
    extra_fields=None,
    ok: bool = True,
    detail: str = "",
) -> str:
    """Shared success/error card for mute/ban/kick/timeout."""
    if not ok:
        return action_card(
            "Failed",
            [
                field_user(target),
                field_by(actor, "Requested By"),
                field_reason(detail or reason),
            ],
            icon=E.ERROR,
        )
    title, icon = _ACTION_META.get(action, ("Action Complete", E.CHECK))
    fields = [
        field_user(target),
        field_by(actor, f"{action.value.upper()}ED BY" if action != WarningAction.KICK else "KICKED BY"),
        field_reason(reason),
        field_duration(str(duration) if duration else None),
    ]
    # Fix awkward double-E: MUTEED etc.
    fields[1] = field_by(
        actor,
        {
            WarningAction.MUTE: "Muted By",
            WarningAction.BAN: "Banned By",
            WarningAction.KICK: "Kicked By",
            WarningAction.TIMEOUT: "Timed Out By",
        }.get(action, "Action By"),
    )
    if extra_fields:
        fields.extend(extra_fields)
    return action_card(title, fields, icon=icon)


def warn_card(
    target: User,
    actor: Optional[User],
    reason: str,
    count: int,
    limit: int,
    *,
    triggered: Optional[str] = None,
    extra_fields=None,
) -> str:
    title = "Warning Limit Reached" if triggered else "Warning Issued"
    fields = [
        field_user(target),
        field_by(actor, "Warned By"),
        field_reason(reason),
        field_count(count, limit),
    ]
    if triggered:
        fields.append(field_extra(E.ALERT, "Action", escape(triggered)))
    if extra_fields:
        fields.extend(extra_fields)
    return action_card(title, fields, icon=E.WARN)


# ============================================
# MUTE COMMANDS
# ============================================


async def mute_command(message: Message, bot: Bot, args: list):
    """Handle /mute command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("mute users", "Can Restrict Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to mute.\n\n"
            "<b>Usage:</b>\n"
            "• /mute @user [period] [reason]\n"
            "• Reply to a message with /mute [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot mute yourself.",
            parse_mode=ParseMode.HTML)
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot mute myself.",
            parse_mode=ParseMode.HTML)
        return

    duration, reason = parse_duration_reason(action_args(message, args))

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.MUTE, duration, reason
    )
    if ok:
        try:
            chat_id = message.chat.id
            await asyncio.to_thread(
                _record_moderation_action, chat_id, target_user.id, "restriction"
            )
        except Exception:
            pass
    card_text = moderation_card(
        WarningAction.MUTE, target_user, message.from_user, reason,
        duration=duration, ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def dmute_command(message: Message, bot: Bot, args: list):
    """Handle /dmute command - mute and delete message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("mute users", "Can Restrict Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        try:
            await message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dmute [period] [reason]\n"
            "• /dmute @user [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot mute yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot mute myself.")
        return

    duration, reason = parse_duration_reason(action_args(message, args))

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.MUTE, duration, reason
    )
    extras = []
    if message_deleted:
        extras.append(field_extra(E.CROSS, "Message", "Deleted"))
    card_text = moderation_card(
        WarningAction.MUTE, target_user, message.from_user, reason,
        duration=duration, extra_fields=extras, ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def smute_command(message: Message, bot: Bot, args: list):
    """Handle /smute command - silent mute."""
    if message.chat.type == "private":
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user or target_user.id == message.from_user.id:
        return

    if target_user.id == bot.id:
        return

    duration, reason = parse_duration_reason(action_args(message, args))

    await execute_action(
        message, bot, target_user.id, WarningAction.MUTE, duration, reason
    )

    try:
        await message.delete()
    except Exception:
        pass


async def tmute_command(message: Message, bot: Bot, args: list):
    """Handle /tmute command - temporary mute."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("mute users", "Can Restrict Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to mute.\n\n"
            "<b>Usage:</b> /tmute @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tmute @user 1h Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot mute yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot mute myself.")
        return

    remaining = action_args(message, args)
    duration, reason = parse_duration_reason(remaining)

    if not duration and remaining:
        await reply_text(message, 
            f"{E.ERROR} Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
        )
        return

    if not duration:
        await reply_text(message, 
            f"{E.ERROR} Duration is required for temporary mute.\n\n"
            "<b>Usage:</b> /tmute @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tmute @user 1h Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.MUTE, duration, reason
    )
    unmute_time = datetime.now() + duration
    card_text = moderation_card(
        WarningAction.MUTE, target_user, message.from_user, reason,
        duration=duration,
        extra_fields=[field_extra(E.TIME, "Auto-unmute", unmute_time.strftime("%Y-%m-%d %H:%M:%S"))],
        ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def unmute_command(message: Message, bot: Bot, args: list):
    """Handle /unmute command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("unmute users", "Can Restrict Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to unmute.\n\n"
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
        await bot.restrict_chat_member(
            message.chat.id, target_user.id, permissions
        )
        await reply_card(
            message,
            action_card(
                "Unmute Successful",
                [
                    field_user(target_user),
                    field_by(message.from_user, "Unmuted By"),
                ],
                icon=E.MUTE,
            ),
            user=target_user,
        )
    except Exception as e:
        await reply_text(message, failed("unmute that user", e, permission="Can Restrict Members"))


# ============================================
# BAN COMMANDS
# ============================================


async def ban_command(message: Message, bot: Bot, args: list):
    """Handle /ban command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("ban users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to ban.\n\n"
            "<b>Usage:</b>\n"
            "• /ban @user [period] [reason]\n"
            "• Reply to a message with /ban [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot ban yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot ban myself.")
        return

    try:
        target_member = await bot.get_chat_member(
            message.chat.id, target_user.id
        )
        if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
            sender_member = await bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
            if sender_member.status != ChatMemberStatus.CREATOR:
                await reply_text(message, 
                    f"{E.ERROR} You cannot ban a user with equal or higher permissions."
                )
                return
    except Exception:
        pass

    duration, reason = parse_duration_reason(action_args(message, args))

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.BAN, duration, reason
    )
    if ok:
        try:
            chat_id = message.chat.id
            await asyncio.to_thread(
                _record_moderation_action, chat_id, target_user.id, "restriction"
            )
        except Exception:
            pass
    card_text = moderation_card(
        WarningAction.BAN, target_user, message.from_user, reason,
        duration=duration, ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def dban_command(message: Message, bot: Bot, args: list):
    """Handle /dban command - ban and delete message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("ban users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        try:
            await message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dban [period] [reason]\n"
            "• /dban @user [period] [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot ban yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot ban myself.")
        return

    duration, reason = parse_duration_reason(action_args(message, args))

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.BAN, duration, reason
    )
    extras = []
    if message_deleted:
        extras.append(field_extra(E.CROSS, "Message", "Deleted"))
    card_text = moderation_card(
        WarningAction.BAN, target_user, message.from_user, reason,
        duration=duration, extra_fields=extras, ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def sban_command(message: Message, bot: Bot, args: list):
    """Handle /sban command - silent ban."""
    if message.chat.type == "private":
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user or target_user.id == message.from_user.id:
        return

    if target_user.id == bot.id:
        return

    duration, reason = parse_duration_reason(action_args(message, args))

    await execute_action(
        message, bot, target_user.id, WarningAction.BAN, duration, reason
    )

    try:
        await message.delete()
    except Exception:
        pass


async def tban_command(message: Message, bot: Bot, args: list):
    """Handle /tban command - temporary ban."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("ban users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to ban.\n\n"
            "<b>Usage:</b> /tban @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tban @user 7d Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot ban yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot ban myself.")
        return

    remaining = action_args(message, args)
    duration, reason = parse_duration_reason(remaining)

    if not duration and remaining:
        await reply_text(message, 
            f"{E.ERROR} Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
        )
        return

    if not duration:
        await reply_text(message, 
            f"{E.ERROR} Duration is required for temporary ban.\n\n"
            "<b>Usage:</b> /tban @user &lt;period&gt; [reason]\n"
            "<b>Example:</b> /tban @user 7d Spamming",
            parse_mode=ParseMode.HTML,
        )
        return

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.BAN, duration, reason
    )
    unban_time = datetime.now() + duration
    card_text = moderation_card(
        WarningAction.BAN, target_user, message.from_user, reason,
        duration=duration,
        extra_fields=[field_extra(E.TIME, "Auto-unban", unban_time.strftime("%Y-%m-%d %H:%M:%S"))],
        ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def unban_command(message: Message, bot: Bot, args: list):
    """Handle /unban command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("unban users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to unban.\n\n"
            "<b>Usage:</b> /unban @user or user ID",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        await bot.unban_chat_member(message.chat.id, target_user.id)
        await reply_card(
            message,
            action_card(
                "Unban Successful",
                [
                    field_user(target_user),
                    field_by(message.from_user, "UNBANNED BY"),
                ],
                icon=E.UNBAN,
            ),
            user=target_user,
        )
    except Exception as e:
        await reply_text(message, failed("unban that user", e, permission="Can Ban Members"))


# ============================================
# KICK COMMANDS
# ============================================


async def kick_command(message: Message, bot: Bot, args: list):
    """Handle /kick command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("kick users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to kick.\n\n"
            "<b>Usage:</b>\n"
            "• /kick @user [reason]\n"
            "• Reply to a message with /kick [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot kick yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot kick myself.")
        return

    try:
        target_member = await bot.get_chat_member(
            message.chat.id, target_user.id
        )
        if target_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
            sender_member = await bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
            if sender_member.status != ChatMemberStatus.CREATOR:
                await reply_text(message, 
                    f"{E.ERROR} You cannot kick a user with equal or higher permissions."
                )
                return
    except Exception:
        pass

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.KICK, reason=reason
    )
    if ok:
        try:
            chat_id = message.chat.id
            await asyncio.to_thread(
                _record_moderation_action, chat_id, target_user.id, "restriction"
            )
        except Exception:
            pass
    card_text = moderation_card(
        WarningAction.KICK, target_user, message.from_user, reason,
        ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def dkick_command(message: Message, bot: Bot, args: list):
    """Handle /dkick command - kick and delete message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        await reply_text(message, 
            bot_rights_error("kick users", "Can Ban Members"),
            parse_mode=ParseMode.HTML,
        )
        return

    target_user = None
    message_deleted = False

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        try:
            await message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dkick [reason]\n"
            "• /dkick @user [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot kick yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot kick myself.")
        return

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    ok, detail = await execute_action(
        message, bot, target_user.id, WarningAction.KICK, reason=reason
    )
    extras = []
    if message_deleted:
        extras.append(field_extra(E.CROSS, "Message", "Deleted"))
    card_text = moderation_card(
        WarningAction.KICK, target_user, message.from_user, reason,
        extra_fields=extras, ok=ok, detail=detail,
    )
    await reply_card(message, card_text, user=target_user)


async def skick_command(message: Message, bot: Bot, args: list):
    """Handle /skick command - silent kick."""
    if message.chat.type == "private":
        return

    if not await is_admin(message, bot):
        return

    if not await is_bot_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user or target_user.id == message.from_user.id:
        return

    if target_user.id == bot.id:
        return

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    await execute_action(
        message, bot, target_user.id, WarningAction.KICK, reason=reason
    )

    try:
        await message.delete()
    except Exception:
        pass


# ============================================
# WARNING COMMANDS
# ============================================


async def warn_command(message: Message, bot: Bot, args: list):
    """Handle /warn command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user to warn.\n\n"
            "<b>Usage:</b>\n"
            "• /warn @user [reason]\n"
            "• Reply to a message with /warn [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot warn yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot warn myself.")
        return

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)
    try:
        await asyncio.to_thread(
            _record_moderation_action, chat_id, target_user.id, "warning"
        )
    except Exception:
        pass

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")

        ok, detail = await execute_action(
            message, bot, target_user.id, action, duration, reason
        )
        action_name = {
            WarningAction.MUTE: "Mute",
            WarningAction.KICK: "Kick",
            WarningAction.BAN: "Ban",
            WarningAction.TIMEOUT: "Timeout",
        }.get(action, action.value.capitalize())
        card_text = warn_card(
            target_user, message.from_user, reason,
            warning_count, settings["warn_limit"],
            triggered=f"{action_name} ({detail})" if ok else f"Failed ({detail})",
        )
        await reset_warnings(chat_id, target_user.id)
    else:
        card_text = warn_card(
            target_user, message.from_user, reason,
            warning_count, settings["warn_limit"],
        )

    await reply_card(message, card_text, user=target_user)


async def dwarn_command(message: Message, bot: Bot, args: list):
    """Handle /dwarn command - warn and delete message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    target_user = None
    message_deleted = False

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        try:
            await message.reply_to_message.delete()
            message_deleted = True
        except Exception:
            pass
    else:
        target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please reply to a message or specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• Reply to a message with /dwarn [reason]\n"
            "• /dwarn @user [reason]",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_user.id == message.from_user.id:
        await reply_text(message, f"{E.ERROR} You cannot warn yourself.")
        return

    if target_user.id == bot.id:
        await reply_text(message, f"{E.ERROR} I cannot warn myself.")
        return

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)
    try:
        await asyncio.to_thread(
            _record_moderation_action, chat_id, target_user.id, "warning"
        )
    except Exception:
        pass

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")

        ok, detail = await execute_action(
            message, bot, target_user.id, action, duration, reason
        )
        action_name = {
            WarningAction.MUTE: "Mute",
            WarningAction.KICK: "Kick",
            WarningAction.BAN: "Ban",
            WarningAction.TIMEOUT: "Timeout",
        }.get(action, action.value.capitalize())
        extras = []
        if message_deleted:
            extras.append(field_extra(E.CROSS, "Message", "Deleted"))
        card_text = warn_card(
            target_user, message.from_user, reason,
            warning_count, settings["warn_limit"],
            triggered=f"{action_name} ({detail})" if ok else f"Failed ({detail})",
            extra_fields=extras,
        )
        await reset_warnings(chat_id, target_user.id)
    else:
        extras = []
        if message_deleted:
            extras.append(field_extra(E.CROSS, "Message", "Deleted"))
        card_text = warn_card(
            target_user, message.from_user, reason,
            warning_count, settings["warn_limit"],
            extra_fields=extras,
        )

    await reply_card(message, card_text, user=target_user)


async def swarn_command(message: Message, bot: Bot, args: list):
    """Handle /swarn command - silent warn."""
    if message.chat.type == "private":
        return

    if not await is_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        return

    if target_user.id == message.from_user.id:
        return

    if target_user.id == bot.id:
        return

    reason = " ".join(action_args(message, args)) or DEFAULT_REASON

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)

    warning_count = await add_warning(chat_id, target_user.id, reason)
    try:
        await asyncio.to_thread(
            _record_moderation_action, chat_id, target_user.id, "warning"
        )
    except Exception:
        pass

    if warning_count >= settings["warn_limit"]:
        action = settings["warn_mode"]
        duration = settings.get("warn_mode_duration")
        await execute_action(message, bot, target_user.id, action, duration, reason)
        await reset_warnings(chat_id, target_user.id)

    try:
        await message.delete()
    except Exception:
        pass


async def warns_command(message: Message, bot: Bot, args: list):
    """Handle /warns command - show user warnings."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    target_user = await get_target_user(message, bot, args)
    if not target_user:
        target_user = message.from_user

    chat_id = message.chat.id
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
                f"• {escape(str(w['reason']))} ({w['timestamp'].strftime('%Y-%m-%d %H:%M')})"
                for w in warnings
            ]
        )
        card_text = action_card(
            "Active Warnings",
            [
                field_user(target_user),
                field_count(len(warnings), settings["warn_limit"]),
                field_extra(E.INFO, "List", warning_list),
            ],
            icon=E.WARN,
        )
    else:
        card_text = action_card(
            "No Active Warnings",
            [field_user(target_user)],
            icon=E.CHECK,
        )

    await reply_card(message, card_text, user=target_user)


async def rmwarn_command(message: Message, bot: Bot, args: list):
    """Handle /rmwarn command - remove latest warning."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user.\n\n"
            "<b>Usage:</b> /rmwarn @user",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)

    success = await remove_latest_warning(chat_id, target_user.id)

    if success:
        warnings = await get_warnings(chat_id, target_user.id)
        try:
            await asyncio.to_thread(_record_reputation, target_user.id, "positive", 1)
        except Exception:
            pass
        card_text = action_card(
            "Warning Removed",
            [
                field_user(target_user),
                field_by(message.from_user, "REMOVED BY"),
                field_count(len(warnings), settings["warn_limit"]),
            ],
            icon=E.CHECK,
        )
    else:
        card_text = action_card(
            "No Warning To Remove",
            [field_user(target_user)],
            icon=E.WARNING,
        )

    await reply_card(message, card_text, user=target_user)


async def resetwarn_command(message: Message, bot: Bot, args: list):
    """Handle /resetwarn command - clear all warnings for user."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    target_user = await get_target_user(message, bot, args)

    if not target_user:
        await reply_text(message, 
            f"{E.ERROR} Please specify a user.\n\n"
            "<b>Usage:</b> /resetwarn @user",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)

    count = await reset_warnings(chat_id, target_user.id)

    if count > 0:
        try:
            await asyncio.to_thread(_record_reputation, target_user.id, "positive", count)
        except Exception:
            pass
        card_text = action_card(
            "Warnings Cleared",
            [
                field_user(target_user),
                field_by(message.from_user, "CLEARED BY"),
                field_count(0, settings["warn_limit"]),
                field_extra(E.INFO, "Removed", str(count)),
            ],
            icon=E.CHECK,
        )
    else:
        card_text = action_card(
            "No Warnings To Clear",
            [field_user(target_user)],
            icon=E.WARNING,
        )

    await reply_card(message, card_text, user=target_user)


async def resetallwarns_command(message: Message, bot: Bot):
    """Handle /resetallwarns command - clear all warnings in chat."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    chat_id = message.chat.id
    count = await reset_all_warnings(chat_id)

    if count > 0:
        card_text = action_card(
            "All Warnings Cleared",
            [
                field_by(message.from_user, "CLEARED BY"),
                field_extra(E.WARN, "Removed", str(count)),
            ],
            icon=E.CHECK,
        )
    else:
        card_text = action_card(
            "No Active Warnings",
            [field_extra(E.INFO, "Chat", escape(message.chat.title or ""))],
            icon=E.INFO,
        )

    await reply_card(message, card_text)


# ============================================
# WARNING CONFIGURATION COMMANDS
# ============================================


async def warnlimit_command(message: Message, bot: Bot, args: list):
    """Handle /warnlimit command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not args:
        chat_id = message.chat.id
        settings = await get_chat_settings(chat_id)
        await reply_text(message, 
            f"{E.SETTINGS} Current warning limit: <b>{settings['warn_limit']}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        limit = int(args[0])
        if limit < 1:
            raise ValueError
    except ValueError:
        await reply_text(message, 
            f"{E.ERROR} Please provide a valid number.\n\n"
            "<b>Usage:</b> /warnlimit &lt;number&gt;\n"
            "<b>Example:</b> /warnlimit 3",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)
    settings["warn_limit"] = limit
    settings_db[chat_id] = settings

    await reply_card(
        message,
        action_card(
            "Warn Limit Updated",
            [
                field_by(message.from_user, "Updated By"),
                field_extra(E.WARN, "Limit", str(limit)),
                field_extra(E.INFO, "Triggers On", get_ordinal(limit)),
            ],
            icon=E.SETTINGS,
        ),
    )


async def warnmode_command(message: Message, bot: Bot, args: list):
    """Handle /warnmode command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.")
        return

    if not await is_admin(message, bot):
        return

    if not args:
        chat_id = message.chat.id
        settings = await get_chat_settings(chat_id)
        await reply_text(message, 
            f"{E.SETTINGS} Current warning mode: <b>{settings['warn_mode'].value}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    mode_str = args[0].lower()
    try:
        mode = WarningAction(mode_str)
    except ValueError:
        await reply_text(message, 
            f"{E.ERROR} Invalid warning mode. Choose from: <b>mute</b>, <b>kick</b>, <b>ban</b>, <b>timeout</b>\n\n"
            "<b>Usage:</b> /warnmode &lt;action&gt; [duration]\n"
            "<b>Example:</b> /warnmode mute 1d",
            parse_mode=ParseMode.HTML,
        )
        return

    duration = None
    if len(args) > 1:
        duration = parse_duration(args[1])
        if not duration:
            await reply_text(message, 
                f"{E.ERROR} Invalid duration format. Use: 30s, 5m, 1h, 2d, or 1w"
            )
            return

    chat_id = message.chat.id
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

    card_text = action_card(
        "Warn Mode Updated",
        [
            field_by(message.from_user, "Updated By"),
            field_extra(E.SETTINGS, "Mode", str(mode.value).capitalize()),
            field_duration(str(duration) if duration else None),
            field_extra(E.INFO, "Effect", mode_descriptions[mode]),
        ],
        icon=E.SETTINGS,
    )

    await reply_card(message, card_text)


async def warntime_command(message: Message, bot: Bot, args: list):
    """Handle /warntime command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await is_admin(message, bot):
        return

    if not args:
        chat_id = message.chat.id
        settings = await get_chat_settings(chat_id)
        if settings["warn_time"]:
            await reply_text(message, 
                f"{E.TIME} Warning expiration: <b>{settings['warn_time']}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await reply_text(message, 
                f"{E.TIME} Warning expiration: <b>Disabled</b> (warnings stay forever)",
                parse_mode=ParseMode.HTML,
            )
        return

    if args[0].lower() == "off":
        chat_id = message.chat.id
        settings = await get_chat_settings(chat_id)
        settings["warn_time"] = None
        settings_db[chat_id] = settings

        await reply_text(message, 
            f"{E.SETTINGS} Warning expiration disabled.\n"
            "Warnings will stay forever until cleared."
        )
        return

    duration = parse_duration(args[0])
    if not duration:
        await reply_text(message, 
            f"{E.ERROR} Invalid duration format. Use: 30s, 5m, 1h, 2d, 1w, or <b>off</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)
    settings["warn_time"] = duration
    settings_db[chat_id] = settings

    await reply_text(message, 
        f"{E.TIME} Warning expiration set to: <b>{duration}</b>\n"
        f"Warnings older than {duration} will stop counting.",
        parse_mode=ParseMode.HTML,
    )


# ============================================
# RULES COMMANDS
# ============================================


async def rules_command(message: Message):
    """Handle /rules command."""
    chat_id = message.chat.id

    if chat_id not in rules_db or not rules_db[chat_id].get("text"):
        await reply_text(message, 
            f"{E.BOOKMARK} No rules have been set for this chat yet.\n"
            "Admins can use /setrules to configure them."
        )
        return

    settings = await get_chat_settings(chat_id)
    rules = rules_db[chat_id]

    if settings.get("private_rules"):
        await reply_text(message, 
            f"{E.BOOKMARK} Rules for this chat\n\n"
            "Click the button below to view the rules in a private message.",
        )
        return

    card_text = f"{E.BOOKMARK} Rules\n\n{rules['text']}"
    await reply_text(message, card_text, parse_mode=ParseMode.HTML)


async def setrules_command(message: Message, bot: Bot, args: list):
    """Handle /setrules command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await is_admin(message, bot):
        return

    if message.reply_to_message:
        replied_text = message.reply_to_message.text
        if replied_text:
            chat_id = message.chat.id
            rules_db[chat_id] = {"text": replied_text}
            # Escaped *after* slicing so a cut entity can't leave the
            # parser with an unbalanced tag.
            preview = escape(replied_text[:500])
            await reply_text(
                message,
                f"{E.CHECK} Rules copied from the replied message!\n\n"
                f"<b>Preview:</b>\n{preview}{'...' if len(replied_text) > 500 else ''}",
                parse_mode=ParseMode.HTML,
            )
            return

    if not args:
        await reply_text(message, 
            f"{E.ERROR} Please provide the rules text.\n\n"
            "<b>Usage:</b> /setrules &lt;text&gt;\n\n"
            "Or reply to a message with /setrules to copy its content.",
            parse_mode=ParseMode.HTML,
        )
        return

    rules_text = " ".join(args)
    chat_id = message.chat.id
    rules_db[chat_id] = {"text": rules_text}

    preview = escape(rules_text[:500])
    await reply_text(
        message,
        f"{E.CHECK} Rules updated successfully!\n\n"
        f"<b>Preview:</b>\n{preview}{'...' if len(rules_text) > 500 else ''}",
        parse_mode=ParseMode.HTML,
    )


async def resetrules_command(message: Message, bot: Bot):
    """Handle /resetrules command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await is_admin(message, bot):
        return

    chat_id = message.chat.id

    if chat_id in rules_db and rules_db[chat_id].get("text"):
        del rules_db[chat_id]
        await reply_text(message, f"{E.CHECK} Rules have been cleared for this chat.",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.WARNING} No rules are currently set for this chat.",
            parse_mode=ParseMode.HTML)


async def privaterules_command(message: Message, bot: Bot, args: list):
    """Handle /privaterules command."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await is_admin(message, bot):
        return

    if not args or args[0].lower() not in ["on", "off"]:
        await reply_text(message, 
            f'{E.ERROR} Please specify "on" or "off".\n\n'
            "<b>Usage:</b> /privaterules &lt;on|off&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = args[0].lower() == "on"
    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)
    settings["private_rules"] = enabled
    settings_db[chat_id] = settings

    if enabled:
        await reply_text(message, 
            f"{E.SETTINGS} Private rules enabled.\n"
            "/rules will now send a button that DMs the rules instead of replying inline."
        )
    else:
        await reply_text(message, 
            f"{E.SETTINGS} Private rules disabled.\n"
            "/rules will now reply with the rules inline."
        )


# ============================================
# MODULE SETUP
# ============================================


def setup() -> list[str]:
    """Register this module's handlers. Returns route descriptions for the log."""
    handlers = []

    # Mute commands
    on("message", mute_command, flt=cmd("mute"))
    on("message", dmute_command, flt=cmd("dmute"))
    on("message", smute_command, flt=cmd("smute"))
    on("message", tmute_command, flt=cmd("tmute"))
    on("message", unmute_command, flt=cmd("unmute"))
    handlers.extend(["/mute", "/dmute", "/smute", "/tmute", "/unmute"])

    # Ban commands
    on("message", ban_command, flt=cmd("ban"))
    on("message", dban_command, flt=cmd("dban"))
    on("message", sban_command, flt=cmd("sban"))
    on("message", tban_command, flt=cmd("tban"))
    on("message", unban_command, flt=cmd("unban"))
    handlers.extend(["/ban", "/dban", "/sban", "/tban", "/unban"])

    # Kick commands
    on("message", kick_command, flt=cmd("kick"))
    on("message", dkick_command, flt=cmd("dkick"))
    on("message", skick_command, flt=cmd("skick"))
    handlers.extend(["/kick", "/dkick", "/skick"])

    # Warning commands
    on("message", warn_command, flt=cmd("warn"))
    on("message", dwarn_command, flt=cmd("dwarn"))
    on("message", swarn_command, flt=cmd("swarn"))
    on("message", warns_command, flt=cmd("warns"))
    on("message", rmwarn_command, flt=cmd("rmwarn"))
    on("message", resetwarn_command, flt=cmd("resetwarn"))
    on("message", resetallwarns_command, flt=cmd("resetallwarns"))
    handlers.extend(["/warn", "/dwarn", "/swarn", "/warns", "/rmwarn", "/resetwarn", "/resetallwarns"])

    # Warning configuration
    on("message", warnlimit_command, flt=cmd("warnlimit"))
    on("message", warnmode_command, flt=cmd("warnmode"))
    on("message", warntime_command, flt=cmd("warntime"))
    handlers.extend(["/warnlimit", "/warnmode", "/warntime"])

    # Rules commands
    on("message", rules_command, flt=cmd("rules"))
    on("message", setrules_command, flt=cmd("setrules"))
    on("message", resetrules_command, flt=cmd("resetrules"))
    on("message", privaterules_command, flt=cmd("privaterules"))
    handlers.extend(["/rules", "/setrules", "/resetrules", "/privaterules"])

    return handlers
