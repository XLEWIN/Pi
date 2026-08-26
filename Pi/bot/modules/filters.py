"""Filters module — Custom keyword/text filters for automatic replies.

Uses SQLite database for storage.
"""

import json
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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


def _get_reply_buttons(buttons_json: str) -> Optional[InlineKeyboardMarkup]:
    """Build InlineKeyboardMarkup from stored JSON buttons."""
    if not buttons_json:
        return None
    try:
        buttons = json.loads(buttons_json)
        if not buttons:
            return None
        kb = []
        row = []
        for btn in buttons:
            row.append(InlineKeyboardButton(btn["text"], url=btn["url"]))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        return InlineKeyboardMarkup(kb)
    except Exception:
        return None


# ── Command handlers ─────────────────────────────────────
async def add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /filter — add a new filter."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to manage filters.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b>\n"
            "• /filter &lt;trigger&gt; — Add filter with text reply\n"
            "• /filter &lt;trigger&gt; — Reply to a message to set as response\n"
            "• /stop &lt;trigger&gt; — Remove a filter",
            parse_mode=ParseMode.HTML,
        )
        return

    trigger = context.args[0].lower()
    chat_id = update.effective_chat.id

    reply_text = None
    buttons = []
    media_type = None
    media_id = None

    if update.message.reply_to_message:
        reply_msg = update.message.reply_to_message
        reply_text = reply_msg.text or reply_msg.caption or ""

        if reply_msg.photo:
            media_type = "photo"
            media_id = reply_msg.photo[-1].file_id
        elif reply_msg.sticker:
            media_type = "sticker"
            media_id = reply_msg.sticker.file_id
        elif reply_msg.document:
            media_type = "document"
            media_id = reply_msg.document.file_id
        elif reply_msg.animation:
            media_type = "animation"
            media_id = reply_msg.animation.file_id
        elif reply_msg.video:
            media_type = "video"
            media_id = reply_msg.video.file_id
        elif reply_msg.voice:
            media_type = "voice"
            media_id = reply_msg.voice.file_id
        elif reply_msg.audio:
            media_type = "audio"
            media_id = reply_msg.audio.file_id

        if reply_msg.reply_markup and hasattr(reply_msg.reply_markup, "inline_keyboard"):
            for row in reply_msg.reply_markup.inline_keyboard:
                for btn in row:
                    if btn.url:
                        buttons.append({"text": btn.text, "url": btn.url})
    elif len(context.args) > 1:
        reply_text = " ".join(context.args[1:])
    else:
        await update.message.reply_text(
            "❌ Provide a trigger word and reply to a message, or add text after the trigger."
        )
        return

    buttons_json = json.dumps(buttons) if buttons else None
    db.add_filter(chat_id, trigger, reply_text, buttons_json, media_type, media_id)

    await update.message.reply_text(
        f"✅ Filter set for <b>{trigger}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop — remove a filter."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You need admin rights to manage filters.")
        return

    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /stop &lt;trigger&gt;", parse_mode=ParseMode.HTML)
        return

    trigger = context.args[0].lower()
    chat_id = update.effective_chat.id

    if db.remove_filter(chat_id, trigger):
        await update.message.reply_text(f"✅ Filter <b>{trigger}</b> removed.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Filter not found.")


async def filters_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /filters — list all filters in chat."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ This command only works in groups.")
        return

    chat_id = update.effective_chat.id
    filters_data = db.get_filters(chat_id)

    if not filters_data:
        await update.message.reply_text("ℹ️ No filters set in this chat.")
        return

    trigger_list = "\n".join([f"• <code>{f['trigger_word']}</code>" for f in filters_data])
    await update.message.reply_text(
        f"📋 <b>Active Filters ({len(filters_data)}):</b>\n{trigger_list}",
        parse_mode=ParseMode.HTML,
    )


async def check_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check incoming messages against filters."""
    if not update.message or update.effective_chat.type == "private":
        return

    chat_id = update.effective_chat.id
    filters_data = db.get_filters(chat_id)
    if not filters_data:
        return

    text = (update.message.text or update.message.caption or "").lower()

    for f in filters_data:
        trigger = f["trigger_word"]
        if trigger in text:
            reply_markup = _get_reply_buttons(f.get("buttons_json"))
            media_type = f.get("media_type")
            media_id = f.get("media_id")
            reply_text = f.get("reply_text", "")

            try:
                if media_type == "photo":
                    await update.message.reply_photo(photo=media_id, caption=reply_text, reply_markup=reply_markup)
                elif media_type == "sticker":
                    await update.message.reply_sticker(sticker=media_id)
                    if reply_text:
                        await update.message.reply_text(reply_text, reply_markup=reply_markup)
                elif media_type == "document":
                    await update.message.reply_document(document=media_id, caption=reply_text, reply_markup=reply_markup)
                elif media_type == "animation":
                    await update.message.reply_animation(animation=media_id, caption=reply_text, reply_markup=reply_markup)
                elif media_type == "video":
                    await update.message.reply_video(video=media_id, caption=reply_text, reply_markup=reply_markup)
                elif media_type == "voice":
                    await update.message.reply_voice(voice=media_id, caption=reply_text, reply_markup=reply_markup)
                elif media_type == "audio":
                    await update.message.reply_audio(audio=media_id, caption=reply_text, reply_markup=reply_markup)
                elif reply_text:
                    await update.message.reply_text(reply_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Filter reply error: {e}")
            break


# ── Module setup ─────────────────────────────────────────
def setup(app: Application) -> list:
    """Register filter commands and message handler."""
    app.add_handler(CommandHandler("filter", add_filter, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("stop", stop_filter, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("filters", filters_list, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, check_filters), group=1)

    return ["filter", "stop", "filters"]
