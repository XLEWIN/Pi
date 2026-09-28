"""Watch Words module - Get notified when watched words are used in chat.

Uses SQLite database for storage.
Sends colored buttons via pure PTB.
"""

import logging

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f, or_f
from aiogram.types import Message

from bot.command_handler import COMMAND
from bot.database import db
from bot.keyboards.colored import btn_url, build_keyboard
from bot.emojis import E, EID
from bot.pipeline import on, GROUPS, cmd
from bot.reply import reply_text

logger = logging.getLogger(__name__)


async def _is_admin(message, bot):
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


def _get_chat_link(chat):
    if chat.username:
        return f"@{chat.username}"
    return chat.title or "Private Chat"


async def watch_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return
    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can manage watch words.",
            parse_mode=ParseMode.HTML)
        return
    if not args:
        await reply_text(
            message,
            f"{E.EYES} <b>Watch Words</b>\n\n"
            "<b>Usage:</b>\n"
            "  /watch &lt;word or phrase&gt; - Add a watch word\n"
            "  /unwatch &lt;word or phrase&gt; - Remove a watch word\n"
            "  /watchlist - List your watched words\n"
            "  /watchmode &lt;copy|forward&gt; - Set delivery mode\n\n"
            "Notifications are sent to your DM when watched words are used.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id
    word = " ".join(args).lower().strip()

    existing = db.get_watch_words(chat_id, admin_id)
    if word in existing:
        await reply_text(message, f"{E.WARNING} <b>{word}</b> is already being watched.", parse_mode=ParseMode.HTML)
        return

    db.add_watch_word(chat_id, admin_id, word)
    await reply_text(
        message,
        f"{E.CHECK} Added <b>{word}</b> to your watch list.\nI'll notify you in DM when someone uses it.",
        parse_mode=ParseMode.HTML,
    )


async def unwatch_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return
    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can manage watch words.",
            parse_mode=ParseMode.HTML)
        return
    if not args:
        await reply_text(message, f"{E.INFO} Usage: /unwatch &lt;word or phrase&gt;", parse_mode=ParseMode.HTML)
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id
    word = " ".join(args).lower().strip()

    if db.remove_watch_word(chat_id, admin_id, word):
        await reply_text(message, f"{E.CHECK} Removed <b>{word}</b> from your watch list.", parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.WARNING} Word not found in your watch list.",
            parse_mode=ParseMode.HTML)


async def watchlist_command(message: Message):
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id
    words = db.get_watch_words(chat_id, admin_id)

    if not words:
        await reply_text(
            message,
            f"{E.INFO} Your watch list is empty.\nUse /watch &lt;word&gt; to add words.",
            parse_mode=ParseMode.HTML,
        )
        return

    word_list = "\n".join([f"  - <code>{w}</code>" for w in sorted(words)])
    await reply_text(
        message,
        f"{E.EYES} <b>Your Watched Words ({len(words)}):</b>\n{word_list}",
        parse_mode=ParseMode.HTML,
    )


async def watchmode_command(message: Message, bot: Bot, args: list):
    if message.chat.type == "private":
        await reply_text(message, f"{E.INFO} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return
    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} Only admins can change watch settings.",
            parse_mode=ParseMode.HTML)
        return
    if not args or args[0].lower() not in ["copy", "forward"]:
        await reply_text(
            message,
            f"{E.SETTINGS} Choose mode: <b>copy</b> or <b>forward</b>\n\n"
            "<b>copy</b> - Formatted log with chat, sender, word, date, message.\n"
            "<b>forward</b> - Forwards the original message.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    admin_id = message.from_user.id
    mode = args[0].lower()
    db.set_watch_mode(chat_id, admin_id, mode)
    await reply_text(message, f"{E.CHECK} Watch mode set to: <b>{mode}</b>", parse_mode=ParseMode.HTML)


async def watch_check(message: Message, bot: Bot):
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    chat = message.chat
    text = (message.text or message.caption or "").lower()

    if not text:
        return

    admins_words = db.get_all_watch_words(chat_id)
    if not admins_words:
        return

    for admin_id, words in admins_words.items():
        for word in words:
            if word in text:
                try:
                    sender = message.from_user
                    sender_name = sender.first_name or "Unknown"
                    sender_id = sender.id
                    msg_date = message.date.strftime("%Y-%m-%d %H:%M:%S UTC") if message.date else "Unknown"
                    chat_link = _get_chat_link(chat)

                    if chat.username:
                        msg_link = f"https://t.me/{chat.username}/{message.message_id}"
                    else:
                        msg_link = None

                    mode = db.get_watch_mode(chat_id, admin_id)

                    if mode == "copy":
                        match_text = text[:300] + ("..." if len(text) > 300 else "")
                        copy_text = (
                            f"<b>Watch Word Alert!</b>\n\n"
                            f"<b>Chat:</b> {chat.title} ({chat_link})\n"
                            f"<b>Sender:</b> <a href='tg://user?id={sender_id}'>{sender_name}</a> (<code>{sender_id}</code>)\n"
                            f"<b>Matched:</b> <code>{word}</code>\n"
                            f"<b>Date:</b> {msg_date}\n\n"
                            f"<b>Message:</b>\n{match_text}"
                        )

                        # Send via Bot API with colored buttons
                        tg_buttons = []
                        if msg_link:
                            tg_buttons.append([btn_url("View Message", msg_link, icon_emoji_id=EID.WATCH)])

                        reply_markup = build_keyboard(tg_buttons) if tg_buttons else None
                        await bot.send_message(
                            chat_id=admin_id,
                            text=copy_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                    else:
                        header = (
                            f"<b>Watch Word Alert!</b>\n"
                            f"Chat: {chat.title}\n"
                            f"Matched: <code>{word}</code>\n"
                        )
                        # plain text fallback if HTML entities in title break parse
                        try:
                            await bot.send_message(chat_id=admin_id, text=header, parse_mode=ParseMode.HTML)
                        except Exception:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"Watch Word Alert!\nChat: {chat.title}\nMatched: {word}",
                            )
                        await message.forward(chat_id=admin_id)

                    break
                except Exception as e:
                    logger.warning(f"Watch notification error: {e}")
                break


def setup() -> list:
    on("message", watch_command, flt=and_f(cmd("watch"), GROUPS))
    on("message", unwatch_command, flt=and_f(cmd("unwatch"), GROUPS))
    on("message", watchlist_command, flt=and_f(cmd("watchlist"), GROUPS))
    on("message", watchmode_command, flt=and_f(cmd("watchmode"), GROUPS))
    on("message", watch_check, group=3, flt=and_f(or_f(F.text, F.caption), ~COMMAND))

    return ["watch", "unwatch", "watchlist", "watchmode"]
