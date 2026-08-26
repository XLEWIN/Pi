"""Watch Words module - Get notified when watched words are used in chat.

Uses SQLite database for storage.
Sends colored buttons via pure PTB.
"""

import logging
from datetime import datetime

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
from bot.keyboards.colored import btn_url, build_keyboard

logger = logging.getLogger(__name__)


async def _is_admin(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False


def _get_chat_link(chat):
    if chat.username:
        return f"@{chat.username}"
    return chat.title or "Private Chat"


async def watch_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can manage watch words.")
        return
    if not context.args:
        await update.message.reply_text(
            "<b>Watch Words</b>\n\n"
            "<b>Usage:</b>\n"
            "  /watch &lt;word or phrase&gt; - Add a watch word\n"
            "  /unwatch &lt;word or phrase&gt; - Remove a watch word\n"
            "  /watchlist - List your watched words\n"
            "  /watchmode &lt;copy|forward&gt; - Set delivery mode\n\n"
            "Notifications are sent to your DM when watched words are used.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    word = " ".join(context.args).lower().strip()

    existing = db.get_watch_words(chat_id, admin_id)
    if word in existing:
        await update.message.reply_text(f"<b>{word}</b> is already being watched.", parse_mode=ParseMode.HTML)
        return

    db.add_watch_word(chat_id, admin_id, word)
    await update.message.reply_text(
        f"Added <b>{word}</b> to your watch list.\nI'll notify you in DM when someone uses it.",
        parse_mode=ParseMode.HTML,
    )


async def unwatch_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can manage watch words.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unwatch &lt;word or phrase&gt;", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    word = " ".join(context.args).lower().strip()

    if db.remove_watch_word(chat_id, admin_id, word):
        await update.message.reply_text(f"Removed <b>{word}</b> from your watch list.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Word not found in your watch list.")


async def watchlist_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    words = db.get_watch_words(chat_id, admin_id)

    if not words:
        await update.message.reply_text(
            "Your watch list is empty.\nUse /watch &lt;word&gt; to add words.",
            parse_mode=ParseMode.HTML,
        )
        return

    word_list = "\n".join([f"  - <code>{w}</code>" for w in sorted(words)])
    await update.message.reply_text(
        f"<b>Your Watched Words ({len(words)}):</b>\n{word_list}",
        parse_mode=ParseMode.HTML,
    )


async def watchmode_command(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return
    if not await _is_admin(update, context):
        await update.message.reply_text("Only admins can change watch settings.")
        return
    if not context.args or context.args[0].lower() not in ["copy", "forward"]:
        await update.message.reply_text(
            "Choose mode: <b>copy</b> or <b>forward</b>\n\n"
            "<b>copy</b> - Formatted log with chat, sender, word, date, message.\n"
            "<b>forward</b> - Forwards the original message.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    admin_id = update.effective_user.id
    mode = context.args[0].lower()
    db.set_watch_mode(chat_id, admin_id, mode)
    await update.message.reply_text(f"Watch mode set to: <b>{mode}</b>", parse_mode=ParseMode.HTML)


async def watch_check(update, context):
    if not update.message or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    chat = update.effective_chat
    message = update.message
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
                            tg_buttons.append([btn_url("View Message", msg_link)])

                        reply_markup = build_keyboard(tg_buttons) if tg_buttons else None
                        await context.bot.send_message(
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
                        await context.bot.send_message(chat_id=admin_id, text=header, parse_mode=ParseMode.HTML)
                        await message.forward(chat_id=admin_id)

                    break
                except Exception as e:
                    logger.error(f"Watch notification error: {e}")
                break


def setup(app: Application) -> list:
    app.add_handler(CommandHandler("watch", watch_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unwatch", unwatch_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("watchlist", watchlist_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("watchmode", watchmode_command, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, watch_check), group=3)

    return ["watch", "unwatch", "watchlist", "watchmode"]
