"""Welcome module — Welcome/Goodbye messages for new and leaving members.

Adapted from boa2 for Pi bot. Enabled by default.
"""

import asyncio
import logging
from html import escape

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import Message

from bot.database import db
from bot.emojis import E
from bot.pipeline import on, GROUPS, cmd
from bot.reply import reply_text

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────
async def _is_admin(message: Message, bot: Bot) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
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
async def setwelcome_command(message: Message, bot: Bot, args: list):
    """Handle /setwelcome — set custom welcome message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can change welcome settings.",
            parse_mode=ParseMode.HTML)
        return

    if not args and not message.reply_to_message:
        await reply_text(
            message,
            f"{E.WAVE} <b>Set Welcome Message</b>\n\n"
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

    text = " ".join(args) if args else ""
    if message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or text

    if not text:
        await reply_text(message, "Please provide welcome text.")
        return

    chat_id = message.chat.id
    await asyncio.to_thread(db.set_welcome_text, chat_id, text)
    await reply_text(message, f"{E.CHECK} Welcome message saved!",
            parse_mode=ParseMode.HTML)


async def setgoodbye_command(message: Message, bot: Bot, args: list):
    """Handle /setgoodbye — set custom goodbye message."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can change goodbye settings.",
            parse_mode=ParseMode.HTML)
        return

    if not args and not message.reply_to_message:
        await reply_text(
            message,
            f"{E.GOODBYE} <b>Set Goodbye Message</b>\n\n"
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

    text = " ".join(args) if args else ""
    if message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or text

    if not text:
        await reply_text(message, f"{E.ERROR} Please provide goodbye text.",
            parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    await asyncio.to_thread(db.set_goodbye_text, chat_id, text)
    await reply_text(message, f"{E.CHECK} Goodbye message saved!",
            parse_mode=ParseMode.HTML)


async def resetwelcome_command(message: Message, bot: Bot):
    """Handle /resetwelcome — reset welcome to default."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        await reply_text(message, "Only admins can reset welcome.")
        return

    await asyncio.to_thread(db.reset_welcome, message.chat.id)
    await reply_text(message, f"{E.CHECK} Welcome message reset to default!",
            parse_mode=ParseMode.HTML)


async def resetgoodbye_command(message: Message, bot: Bot):
    """Handle /resetgoodbye — reset goodbye to default."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        await reply_text(message, "Only admins can reset goodbye.")
        return

    await asyncio.to_thread(db.reset_goodbye, message.chat.id)
    await reply_text(message, f"{E.CHECK} Goodbye message reset to default!",
            parse_mode=ParseMode.HTML)


async def welcome_command(message: Message, bot: Bot, args: list):
    """Handle /welcome — toggle or view welcome settings."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        await reply_text(message, "Only admins can manage welcome settings.")
        return

    chat_id = message.chat.id
    settings = await asyncio.to_thread(db.get_welcome_settings, chat_id)
    msg = await asyncio.to_thread(db.get_welcome_message, chat_id)

    if args:
        arg = args[0].lower()
        if arg == "on":
            await asyncio.to_thread(db.set_welcome_enabled, chat_id, True)
            await reply_text(message, f"{E.CHECK} Welcome messages enabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "off":
            await asyncio.to_thread(db.set_welcome_enabled, chat_id, False)
            await reply_text(message, f"{E.CROSS} Welcome messages disabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "noformat":
            await reply_text(
                message,
                f"{E.WAVE} <b>Welcome Settings:</b>\n"
                f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
                f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n\n"
                f"<b>Welcome text (no formatting):</b>\n{msg.get('welcome_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await reply_text(
        message,
        f"{E.WAVE} <b>Welcome Settings:</b>\n"
        f"  Welcome: {'ON' if settings.get('welcome_enabled') else 'OFF'}\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Welcome:</b>\n{msg.get('welcome_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def goodbye_command(message: Message, bot: Bot, args: list):
    """Handle /goodbye — toggle or view goodbye settings."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    if not await _is_admin(message, bot):
        await reply_text(message, "Only admins can manage goodbye settings.")
        return

    chat_id = message.chat.id
    settings = await asyncio.to_thread(db.get_welcome_settings, chat_id)
    msg = await asyncio.to_thread(db.get_welcome_message, chat_id)

    if args:
        arg = args[0].lower()
        if arg == "on":
            await asyncio.to_thread(db.set_goodbye_enabled, chat_id, True)
            await reply_text(message, f"{E.CHECK} Goodbye messages enabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "off":
            await asyncio.to_thread(db.set_goodbye_enabled, chat_id, False)
            await reply_text(message, f"{E.CROSS} Goodbye messages disabled!",
            parse_mode=ParseMode.HTML)
            return
        elif arg == "noformat":
            await reply_text(
                message,
                f"{E.GOODBYE} <b>Goodbye Settings:</b>\n"
                f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n\n"
                f"<b>Goodbye text (no formatting):</b>\n{msg.get('goodbye_text', '')}",
                parse_mode=ParseMode.HTML,
            )
            return

    await reply_text(
        message,
        f"{E.GOODBYE} <b>Goodbye Settings:</b>\n"
        f"  Goodbye: {'ON' if settings.get('goodbye_enabled') else 'OFF'}\n"
        f"  Clean Goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}\n\n"
        f"<b>Current Goodbye:</b>\n{msg.get('goodbye_text', '')}",
        parse_mode=ParseMode.HTML,
    )


async def cleanwelcome_command(message: Message, bot: Bot, args: list):
    """Handle /cleanwelcome — toggle clean welcome."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can change this setting.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        settings = await asyncio.to_thread(db.get_welcome_settings, message.chat.id)
        await reply_text(message, f"{E.SETTINGS} Clean welcome: {'ON' if settings.get('clean_welcome') else 'OFF'}",
            parse_mode=ParseMode.HTML)
        return

    arg = args[0].lower()
    if arg == "on":
        await asyncio.to_thread(db.set_clean_welcome, message.chat.id, True)
        await reply_text(message, f"{E.CHECK} Clean welcome enabled! Old welcome messages will be deleted.",
            parse_mode=ParseMode.HTML)
    elif arg == "off":
        await asyncio.to_thread(db.set_clean_welcome, message.chat.id, False)
        await reply_text(message, f"{E.CROSS} Clean welcome disabled!",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.INFO} Usage: /cleanwelcome on|off",
            parse_mode=ParseMode.HTML)


async def cleangoodbye_command(message: Message, bot: Bot, args: list):
    """Handle /cleangoodbye — toggle clean goodbye."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can change this setting.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        settings = await asyncio.to_thread(db.get_welcome_settings, message.chat.id)
        await reply_text(message, f"{E.SETTINGS} Clean goodbye: {'ON' if settings.get('clean_goodbye') else 'OFF'}",
            parse_mode=ParseMode.HTML)
        return

    arg = args[0].lower()
    if arg == "on":
        await asyncio.to_thread(db.set_clean_goodbye, message.chat.id, True)
        await reply_text(message, f"{E.CHECK} Clean goodbye enabled! Old goodbye messages will be deleted.",
            parse_mode=ParseMode.HTML)
    elif arg == "off":
        await asyncio.to_thread(db.set_clean_goodbye, message.chat.id, False)
        await reply_text(message, f"{E.CROSS} Clean goodbye disabled!",
            parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.INFO} Usage: /cleangoodbye on|off",
            parse_mode=ParseMode.HTML)


# ── Welcome/Goodbye handlers ─────────────────────────────
async def new_member_handler(message: Message, bot: Bot):
    """Handle new members joining the chat."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    settings = await asyncio.to_thread(db.get_welcome_settings, chat_id)

    if not settings.get("welcome_enabled", True):
        return

    msg_data = await asyncio.to_thread(db.get_welcome_message, chat_id)
    welcome_text = msg_data.get("welcome_text", f"{E.WAVE} Hey {{first}}, welcome to {{chatname}}!")

    for user in message.new_chat_members:
        # Skip bots
        if user.is_bot:
            continue

        # Skip if user is the bot itself
        if user.id == bot.id:
            continue

        # Clean old welcome message
        if settings.get("clean_welcome") and settings.get("last_welcome_msg_id"):
            try:
                await bot.delete_message(chat_id, settings["last_welcome_msg_id"])
            except Exception:
                pass

        # Format and send welcome
        text = format_welcome(welcome_text, user, message.chat)

        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await asyncio.to_thread(db.update_last_welcome_msg, chat_id, sent.message_id)
        except Exception as e:
            logger.warning(f"Welcome message error: {e}")


async def left_member_handler(message: Message, bot: Bot):
    """Handle members leaving the chat."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    settings = await asyncio.to_thread(db.get_welcome_settings, chat_id)

    if not settings.get("goodbye_enabled", True):
        return

    user = message.left_chat_member
    if not user or user.is_bot:
        return

    msg_data = await asyncio.to_thread(db.get_welcome_message, chat_id)
    goodbye_text = msg_data.get("goodbye_text", f"{E.GOODBYE} Sad to see you leaving {{first}}. Take Care!")

    # Clean old goodbye message
    if settings.get("clean_goodbye") and settings.get("last_goodbye_msg_id"):
        try:
            await bot.delete_message(chat_id, settings["last_goodbye_msg_id"])
        except Exception:
            pass

    text = format_welcome(goodbye_text, user, message.chat)

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.to_thread(db.update_last_goodbye_msg, chat_id, sent.message_id)
    except Exception as e:
        logger.warning(f"Goodbye message error: {e}")


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register welcome commands and handlers."""
    # Commands
    on("message", setwelcome_command, flt=and_f(cmd("setwelcome"), GROUPS))
    on("message", setgoodbye_command, flt=and_f(cmd("setgoodbye"), GROUPS))
    on("message", resetwelcome_command, flt=and_f(cmd("resetwelcome"), GROUPS))
    on("message", resetgoodbye_command, flt=and_f(cmd("resetgoodbye"), GROUPS))
    on("message", welcome_command, flt=and_f(cmd("welcome"), GROUPS))
    on("message", goodbye_command, flt=and_f(cmd("goodbye"), GROUPS))
    on("message", cleanwelcome_command, flt=and_f(cmd("cleanwelcome"), GROUPS))
    on("message", cleangoodbye_command, flt=and_f(cmd("cleangoodbye"), GROUPS))

    # Welcome/Goodbye handlers
    on("message", new_member_handler, group=10, flt=F.new_chat_members)
    on("message", left_member_handler, group=10, flt=F.left_chat_member)

    return ["/setwelcome", "/setgoodbye", "/resetwelcome", "/resetgoodbye",
            "/welcome", "/goodbye", "/cleanwelcome", "/cleangoodbye"]
