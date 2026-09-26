"""Users module — User registration, tracking, and logging.

Tracks all users in groups and DMs, logs activity to SQLite.
"""

import asyncio
import logging
from datetime import datetime
from html import escape
from typing import Optional

from telegram import Update, ChatMember, User
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    ChatMemberHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

from bot.command_handler import COMMAND, CommandHandler
from bot.database import db
from bot.modules.start import send_log, format_user_log
from bot.emojis import E, EID, custom_emoji
from bot.keyboards.colored import btn_danger, btn_primary, build_keyboard
from bot.responses import action_card, field_extra, rank_value

logger = logging.getLogger(__name__)


def get_user_display(user: User) -> str:
    """Get display name for a user."""
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


async def register_user(user: User, context: ContextTypes.DEFAULT_TYPE,
                        chat_id: int = None, chat_title: str = None,
                        action: str = "joined"):
    """Register a user and log the activity (never blocks the caller for long)."""
    if user.is_bot:
        return

    log_message = format_user_log(user, action, chat_title)

    def _db_work() -> None:
        try:
            db.add_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
            )
            if chat_id and chat_title:
                db.add_group(chat_id, chat_title)
                db.add_group_member(chat_id, user.id, "member")
            db.update_user_activity(
                user_id=user.id,
                action=action,
                chat_id=chat_id,
                chat_title=chat_title,
            )
        except Exception as e:
            logger.warning(f"register_user DB failed: {e}")

    # Off the event loop so join events cannot stall command replies.
    asyncio.get_running_loop().run_in_executor(None, _db_work)
    # Soft log — timeout + HTML-safe payload already inside send_log/format_user_log.
    asyncio.create_task(send_log(context, log_message))
    logger.info(f"User {user.id} ({get_user_display(user)}) {action}")


async def register_group_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register all members when bot is added to a group."""
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or chat.first_name or "Unknown"

    # Check if bot was added
    bot_added = any(member.id == context.bot.id for member in update.message.new_chat_members)

    if bot_added:
        from html import escape
        safe_title = escape(str(chat_title))

        def _db_work() -> None:
            try:
                bot_user = context.bot_data.get("_me")
                # Fallback identity is filled below if missing; group rows only need chat.
                db.add_group(chat_id, str(chat_title))
                if bot_user:
                    db.add_user(
                        user_id=bot_user["id"],
                        username=bot_user.get("username"),
                        first_name=bot_user.get("first_name"),
                        is_bot=True,
                    )
                    db.add_group_member(chat_id, bot_user["id"], "bot")
            except Exception as e:
                logger.warning(f"bot-added DB failed: {e}")

        me = context.bot_data.get("username")
        # Keep identity in bot_data for background DB work.
        try:
            bot_user = await context.bot.get_me()
            context.bot_data["_me"] = {
                "id": bot_user.id,
                "username": bot_user.username,
                "first_name": bot_user.first_name,
            }
        except Exception:
            bot_user = None

        asyncio.get_running_loop().run_in_executor(None, _db_work)

        log_message = (
            f"🤖 <b>Bot added to chat</b>\n"
            f"💬 Chat: <b>{safe_title}</b>\n"
            f"🆔 Chat ID: <code>{chat_id}</code>"
        )
        asyncio.create_task(send_log(context, log_message))
        logger.info(f"Bot added to group {chat_title} ({chat_id})")

        # Group stats are informational — fire and forget.
        async def _stats() -> None:
            try:
                member_count = await asyncio.wait_for(
                    context.bot.get_chat_member_count(chat_id), timeout=5
                )
                await send_log(
                    context,
                    f"📊 <b>Group stats</b>\n"
                    f"💬 Chat: <b>{safe_title}</b>\n"
                    f"👥 Members: {member_count}",
                )
            except Exception as e:
                logger.warning(f"Group stats log failed: {e}")

        asyncio.create_task(_stats())

    # Register other new members without blocking this handler.
    for member in update.message.new_chat_members:
        if not member.is_bot:
            asyncio.create_task(
                register_user(
                    member, context,
                    chat_id=chat_id,
                    chat_title=chat_title,
                    action=f"joined {chat_title}",
                )
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
    """Track messages to register active users (DB work runs in the background)."""
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    chat = update.effective_chat

    if user.is_bot:
        return

    def _db_work() -> None:
        try:
            db.add_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
            )
            db.update_user_activity(
                user_id=user.id,
                action="sent message",
                chat_id=chat.id,
                chat_title=chat.title or chat.first_name,
            )
        except Exception as e:
            logger.warning(f"track_message DB failed: {e}")

    # Don't block the event loop — SQLite commits can stall command replies.
    asyncio.get_running_loop().run_in_executor(None, _db_work)


async def userstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /userstats — show bot statistics."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    user_count = db.get_user_count()
    group_count = db.get_group_count()

    stats_text = f"""{E.CHART} Bot Statistics

{E.USER} Total Users: {user_count}
💬 Total Groups: {group_count}

Use /myinfo to see your info.
Use /recentactivity to see recent activity."""

    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)


async def _build_info_text(bot, target, chat_id: Optional[int] = None) -> str:
    """Full user-info card (bot tree style) with real data sources.

    Fields without a Bot API source show n/a:
    * DC ID — Bot API does not expose the data center.
    * Custom Bio / Custom Tag — Pi has no such profile fields.
    * AFK Status — Pi has no AFK system.
    Health is derived from real moderation data:
    ``100 − 25 × warnings`` (each warning costs 25%).
    Messages/ranks derive from daily_messages (same numbers as /rank);
    pass ``chat_id`` (groups only) to include the Chat Rank field.
    """
    uid = target.id
    first = getattr(target, "first_name", None) or "n/a"
    last = getattr(target, "last_name", None) or "n/a"
    name = " ".join(
        p
        for p in (getattr(target, "first_name", None), getattr(target, "last_name", None))
        if p
    ) or str(uid)
    mention = f'<a href="tg://user?id={uid}">{escape(name)}</a>'
    username_raw = getattr(target, "username", None)
    username = f"@{escape(username_raw)}" if username_raw else "n/a"

    # Bio: available on ChatFullInfo for private chats.
    bio = "n/a"
    try:
        chat = await bot.get_chat(uid)
        raw_bio = getattr(chat, "bio", None)
        if raw_bio:
            bio = escape(raw_bio)
    except Exception:
        pass

    photos = "n/a"
    try:
        profile = await bot.get_user_profile_photos(uid, limit=1)
        photos = str(profile.total_count)
    except Exception:
        pass
    photos_display = (
        photos if photos == "n/a"
        else f"{photos} photo{'s' if photos != '1' else ''}"
    )

    user_row = db.get_user(uid) or {}
    warnings = int(user_row.get("warnings") or 0)
    health = max(0, 100 - 25 * min(warnings, 4))
    filled = health // 10
    bar = "▰" * filled + "▱" * (10 - filled)

    # Unified rank info — same source as /rank, /rankings and /profile.
    rank_info = db.get_user_rank_info(uid, chat_id)

    fields = [
        field_extra(custom_emoji("💭", EID.INFO), "ID", f"<code>{uid}</code>"),
        field_extra(E.USER, "First Name", escape(first)),
        field_extra(E.USER, "Last Name", escape(last)),
        field_extra(E.ANNOUNCE, "Username", username),
        field_extra(E.WAVE, "Mention", mention),
        field_extra(E.WEB, "DC ID", "n/a"),
        field_extra(E.BOOKMARK, "Bio", bio),
        field_extra(E.SPARKLE, "Custom Bio", "n/a"),
        field_extra(E.LOCATION, "Custom Tag", "n/a"),
        field_extra(E.WATCH, "Profile Photos", photos_display),
        field_extra(E.HEART, "Health", f"{health}% {bar}"),
        field_extra(E.INFO, "Messages", f"{rank_info['global_messages']:,}"),
    ]
    if chat_id is not None:
        fields.append(field_extra(
            E.CROWN, "Chat Rank",
            rank_value(rank_info["chat_rank"], rank_info["chat_position"],
                       rank_info["chat_members"], rank_info["chat_messages"]),
        ))
    fields.extend([
        field_extra(
            E.WEB, "Global Rank",
            rank_value(rank_info["global_rank"], rank_info["global_position"],
                       rank_info["global_members"], rank_info["global_messages"]),
        ),
        field_extra(E.TIME, "AFK Status", "No"),
        field_extra(E.FOLDER, "Common Groups", str(db.count_user_groups(uid))),
        field_extra(E.CROSS, "Globally Banned", "Yes" if db.is_gbanned(uid) else "No"),
        field_extra(E.MUTE, "Globally Muted", "Yes" if user_row.get("is_muted") else "No"),
    ])

    return action_card(
        "User Information",
        fields,
        icon=E.USER,
    )


def info_keyboard():
    """Colored My Info / Close row under the info card."""
    return build_keyboard(
        [
            [
                btn_primary("My Info", "info:me", icon_emoji_id=EID.USER),
                btn_danger("Close", "info:close", icon_emoji_id=EID.CROSS),
            ]
        ]
    )


def _chat_scope_id(update) -> Optional[int]:
    """Group chat id for rank lookups, or None in private chats."""
    chat = getattr(update, "effective_chat", None)
    if chat is None or getattr(chat, "type", None) in (None, "private"):
        return None
    return getattr(chat, "id", None)


async def _send_info(update: Update, context: ContextTypes.DEFAULT_TYPE, target) -> None:
    text = await _build_info_text(context.bot, target, chat_id=_chat_scope_id(update))
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=info_keyboard()
    )


async def _resolve_info_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, default_self: bool
):
    """Reply → @mention → numeric ID → (default_self ? self : None)."""
    message = update.message
    if (
        message is not None
        and message.reply_to_message
        and message.reply_to_message.from_user
    ):
        return message.reply_to_message.from_user

    if context.args:
        arg = context.args[0]
        if arg.startswith("@"):
            try:
                member = await context.bot.get_chat_member(
                    update.effective_chat.id, arg
                )
                if member and member.user:
                    return member.user
            except Exception:
                pass
            return None
        try:
            uid = int(arg)
        except ValueError:
            return None
        if uid == update.effective_user.id:
            return update.effective_user
        try:
            chat = await context.bot.get_chat(uid)
            if getattr(chat, "type", None) != "private":
                return None
            return chat
        except Exception:
            return None

    return update.effective_user if default_self else None


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /info — full user-info card (self when no target given)."""
    target = await _resolve_info_target(update, context, default_self=True)
    if target is None:
        await update.message.reply_text(
            f"{E.ERROR} Could not find that user.\n\n"
            "<b>Usage:</b>\n"
            "• /info — your own info\n"
            "• /info @user or /info USER_ID\n"
            "• Reply to a message with /info",
            parse_mode=ParseMode.HTML,
        )
        return
    await _send_info(update, context, target)


async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myinfo — the sender's own info card."""
    await _send_info(update, context, update.effective_user)


async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /userinfo @user — another user's info card (target required)."""
    target = await _resolve_info_target(update, context, default_self=False)
    if target is None:
        await update.message.reply_text(
            f"{E.ERROR} Please specify a user.\n\n"
            "<b>Usage:</b>\n"
            "• /userinfo @user or /userinfo USER_ID\n"
            "• Reply to a message with /userinfo",
            parse_mode=ParseMode.HTML,
        )
        return
    await _send_info(update, context, target)


async def info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle info:* callbacks — My Info (re-render for clicker) / Close."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("info:"):
        return
    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    if action == "me":
        await query.answer()
        try:
            # Chat scope from the message the button sits on (groups only).
            cb_chat = getattr(query.message, "chat", None)
            cb_chat_id = None
            if cb_chat is not None and getattr(cb_chat, "type", None) not in (None, "private"):
                cb_chat_id = getattr(cb_chat, "id", None)
            text = await _build_info_text(context.bot, query.from_user, chat_id=cb_chat_id)
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=info_keyboard()
            )
        except Exception as e:
            logger.debug(f"info:me render failed: {e}")
        return

    await query.answer("Unknown option", show_alert=True)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /id — chat ID and your user ID (plus reply target if any)."""
    chat = update.effective_chat
    user = update.effective_user
    fields = [
        field_extra(E.FOLDER, "Chat ID", f"<code>{chat.id}</code>"),
        field_extra(E.USER, "Your ID", f"<code>{user.id}</code>"),
    ]

    reply = update.message.reply_to_message if update.message else None
    if reply and reply.from_user:
        target = reply.from_user
        target_name = escape(target.full_name or str(target.id))
        mention = f'<a href="tg://user?id={target.id}">{target_name}</a>'
        fields.append(
            field_extra(E.WATCH, "Target", f"<code>{target.id}</code> · {mention}")
        )

    await update.message.reply_text(
        action_card("ID", fields, icon=E.INFO),
        parse_mode=ParseMode.HTML,
    )


async def recentactivity_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recentactivity — show recent activity."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"{E.ERROR} This command can only be used in groups.",
            parse_mode=ParseMode.HTML)
        return

    # Check if user is admin
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            await update.message.reply_text(f"{E.ERROR} You need admin permissions to use this command.",
            parse_mode=ParseMode.HTML)
            return
    except Exception:
        await update.message.reply_text(f"{E.ERROR} Error checking permissions.",
            parse_mode=ParseMode.HTML)
        return

    activity = db.get_recent_activity(limit=5)

    if not activity:
        await update.message.reply_text(f"{E.INFO} No recent activity.",
            parse_mode=ParseMode.HTML)
        return

    activity_text = f"{E.SETTINGS} Recent Activity\n\n"

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

    # Track all messages to register active users. Own group (19): PTB
    # runs max ONE handler per group — in group 0 it was shadowed by
    # antispam/chatstats for every plain group message.
    app.add_handler(
        MessageHandler(filters.ALL & ~COMMAND, track_message), group=19
    )

    # Commands
    app.add_handler(CommandHandler("userstats", userstats_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("userinfo", userinfo_command))
    app.add_handler(CommandHandler("recentactivity", recentactivity_command))
    app.add_handler(CallbackQueryHandler(info_callback, pattern=r"^info:"))

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
