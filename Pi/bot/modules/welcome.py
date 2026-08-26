"""Welcome module — Welcome/Goodbye messages for new and leaving members.

Adapted from boa2 for Pi bot. Enabled by default.
"""

import logging
from html import escape

from telegram import Update, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from bot.database import db

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────
async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


def format_welcome(text: str, user, chat) -> str:
    """Format welcome/goodbye text with variables."""
    if not text:
        return text

    first = escape(user.first_name or "User")
    last = escape(user.last_name or user.first_name or "User")
    fullname = escape(user.full_name or user.first_name or "User")
    username = f"@{escape(user.username)}" if user.username else first
    mention = f"<a href='tg://user?id={user.id}'>{first}</a>"
    chatname = escape(chat.title) if chat.type != "private" else first
    user_id = user.id

    try:
        formatted = text.format(
            first=first,
            last=last,
            fullname=fullname,
            username=username,
            mention=mention,
            chatname=chatname,
            id=user_id,
        )
        return formatted
    except (KeyError, IndexError):
        return text


# ── Command handlers ─────────────────────────────────────
async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setwelcome — set custom welcome message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can change welcome settings.")
        return

    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text(
            "<b>Set Welcome Message</b>\n\n"
            "<b>Usage:</b>\n"
            "  /setwelcome &lt;text&gt; — Set welcome text\n"
            "  Reply to a message with /setwelcome\n\n"
            "<b>Variables:</b>\n"
            "  {'first'} — First name\n"
            "  {'last'} — Last name\n"
            "  {'fullname'} — Full name\n"
            "  {'username'} — Username\n"
            "  {'mention'} — Mention link\n"
            "  {'chatname'} — Chat name\n"
            "  {'id'} — User ID",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(context.args) if context.args else ""
    if update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or text

    if not text:
        await update.message.reply_text("Please provide welcome text.")
        return

    chat_id = update.effective_chat.id
    db.set_welcome_text(chat_id, text)
    await update.message.reply_text("Welcome message saved!")


async def setgoodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setgoodbye — set custom goodbye message."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can change goodbye settings.")
        return

    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text(
            "<b>Set Goodbye Message</b>\n\n"
            "<b>Usage:</b>\n"
            "  /setgoodbye &lt;text&gt; — Set goodbye text\n"
            "  Reply to a message with /setgoodbye\n\n"
            "<b>Variables:</b>\n"
            "  {'first'} — First name\n"
            "  {'last'} — Last name\n"
            "  {'fullname'} — Full name\n"
            "  {'username'} — Username\n"
            "  {'mention'} — Mention link\n"
            "  {'chatname'} — Chat name\n"
            "  {'id'} — User ID",
            parse_mode=ParseMode.HTML,
        )
        return

    text = " ".join(context.args) if context.args else ""
    if update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or text

    if not text:
        await update.message.reply_text("Please provide goodbye text.")
        return

    chat_id = update.effective_chat.id
    db.set_goodbye_text(chat_id, text)
    await update.message.reply_text("Goodbye message saved!")


async def resetwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetwelcome — reset welcome to default."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can reset welcome.")
        return

    db.reset_welcome(update.effective_chat.id)
    await update.message.reply_text("Welcome message reset to default!")


async def resetgoodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /resetgoodbye — reset goodbye to default."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can reset goodbye.")
        return

    db.reset_goodbye(update.effective_chat.id)
    await update.message.reply_text("Goodbye message reset to default!")


async def welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /welcome — toggle or view welcome settings."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can manage welcome settings.")
        return

    chat_id = update.effective_chat.id
    settings = db.get_welcome_settings(chat_id)
    msg = db.get_welcome_message(chat_id)

    if context.args:
        arg = context.args[0].lower()
        if arg == "on":
            db.set_welcome_enabled(chat_id, True)
            await update.message.reply_text("Welcome messages enabled!")
            return
        elif arg == "off":
            db.set_welcome_enabled(chat_id, False)
            await update.message.reply_text("Welcome messages disabled!")
            return
        elif arg == "noformat":
            await update.message.reply_text(
                f"<b>Welcome Settings:</b>\n"
                f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
                f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n\n"
                f"<b>Welcome text (no formatting):</b>\n{msg.get('welcome_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await update.message.reply_text(
        f"<b>Welcome Settings:</b>\n"
        f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Welcome:</b>\n{msg.get('welcome_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def goodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /goodbye — toggle or view goodbye settings."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can manage goodbye settings.")
        return

    chat_id = update.effective_chat.id
    settings = db.get_welcome_settings(chat_id)
    msg = db.get_welcome_message(chat_id)

    if context.args:
        arg = context.args[0].lower()
        if arg == "on":
            db.set_goodbye_enabled(chat_id, True)
            await update.message.reply_text("Goodbye messages enabled!")
            return
        elif arg == "off":
            db.set_goodbye_enabled(chat_id, False)
            await update.message.reply_text("Goodbye messages disabled!")
            return
        elif arg == "noformat":
            await update.message.reply_text(
                f"<b>Goodbye Settings:</b>\n"
                f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n\n"
                f"<b>Goodbye text (no formatting):</b>\n{msg.get('goodbye_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await update.message.reply_text(
        f"<b>Goodbye Settings:</b>\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Goodbye:</b>\n{msg.get('goodbye_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def cleanwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cleanwelcome — toggle clean welcome."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can change this setting.")
        return

    if not context.args:
        settings = db.get_welcome_settings(update.effective_chat.id)
        await update.message.reply_text(f"Clean welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}")
        return

    arg = context.args[0].lower()
    if arg == "on":
        db.set_clean_welcome(update.effective_chat.id, True)
        await update.message.reply_text("Clean welcome enabled! Old welcome messages will be deleted.")
    elif arg == "off":
        db.set_clean_welcome(update.effective_chat.id, False)
        await update.message.reply_text("Clean welcome disabled!")
    else:
        await update.message.reply_text("Usage: /cleanwelcome on|off")


async def cleangoodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cleangoodbye — toggle clean goodbye."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can change this setting.")
        return

    if not context.args:
        settings = db.get_welcome_settings(update.effective_chat.id)
        await update.message.reply_text(f"Clean goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}")
        return

    arg = context.args[0].lower()
    if arg == "on":
        db.set_clean_goodbye(update.effective_chat.id, True)
        await update.message.reply_text("Clean goodbye enabled! Old goodbye messages will be deleted.")
    elif arg == "off":
        db.set_clean_goodbye(update.effective_chat.id, False)
        await update.message.reply_text("Clean goodbye disabled!")
    else:
        await update.message.reply_text("Usage: /cleangoodbye on|off")


# ── Welcome/Goodbye handlers ─────────────────────────────
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new members joining the chat."""
    if not update.message or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    settings = db.get_welcome_settings(chat_id)

    if not settings.get("welcome_enabled", True):
        return

    msg_data = db.get_welcome_message(chat_id)
    welcome_text = msg_data.get("welcome_text", "Hey {first}, welcome to {chatname}! 👋")

    for user in update.message.new_chat_members:
        # Skip bots
        if user.is_bot:
            continue

        # Skip if user is the bot itself
        if user.id == context.bot.id:
            continue

        # Clean old welcome message
        if settings.get("clean_welcome") and settings.get("last_welcome_msg_id"):
            try:
                await context.bot.delete_message(chat_id, settings["last_welcome_msg_id"])
            except Exception:
                pass

        # Format and send welcome
        text = format_welcome(welcome_text, user, update.effective_chat)

        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            db.update_last_welcome_msg(chat_id, sent.message_id)
        except Exception as e:
            logger.error(f"Welcome message error: {e}")


async def left_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle members leaving the chat."""
    if not update.message or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    settings = db.get_welcome_settings(chat_id)

    if not settings.get("goodbye_enabled", True):
        return

    user = update.message.left_chat_member
    if not user or user.is_bot:
        return

    msg_data = db.get_welcome_message(chat_id)
    goodbye_text = msg_data.get("goodbye_text", "Sad to see you leaving {first}. Take Care! 👋")

    # Clean old goodbye message
    if settings.get("clean_goodbye") and settings.get("last_goodbye_msg_id"):
        try:
            await context.bot.delete_message(chat_id, settings["last_goodbye_msg_id"])
        except Exception:
            pass

    text = format_welcome(goodbye_text, user, update.effective_chat)

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        db.update_last_goodbye_msg(chat_id, sent.message_id)
    except Exception as e:
        logger.error(f"Goodbye message error: {e}")


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register welcome commands and handlers."""
    # Commands
    app.add_handler(CommandHandler("setwelcome", setwelcome_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("resetwelcome", resetwelcome_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("resetgoodbye", resetgoodbye_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("welcome", welcome_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("goodbye", goodbye_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("cleanwelcome", cleanwelcome_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("cleangoodbye", cleangoodbye_command, filters=filters.ChatType.GROUPS))

    # Welcome/Goodbye handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler), group=10)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, left_member_handler), group=10)

    return ["/setwelcome", "/setgoodbye", "/resetwelcome", "/resetgoodbye",
            "/welcome", "/goodbye", "/cleanwelcome", "/cleangoodbye"]
