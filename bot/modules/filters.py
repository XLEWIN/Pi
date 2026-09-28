"""Filters module — Custom keyword/text filters for automatic replies.

Uses SQLite database for storage.
"""

import asyncio
import json
import logging
from typing import Optional

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f, or_f
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.command_handler import COMMAND
from bot.database import db
from bot.emojis import E
from bot.pipeline import GROUPS, cmd, on
from bot.reply import (reply_text, reply_photo, reply_document, reply_animation,
                       reply_video, reply_sticker, reply_voice, reply_audio)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────
async def _is_admin(message, bot) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id
    try:
        member = await bot.get_chat_member(chat_id, user_id)
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
            row.append(InlineKeyboardButton(text=btn["text"], url=btn["url"]))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        return InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception:
        return None


# ── Command handlers ─────────────────────────────────────
async def add_filter(message: Message, bot: Bot, args: list):
    """Handle /filter — add a new filter."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights to manage filters.",
            parse_mode=ParseMode.HTML)
        return

    if not args or len(args) < 1:
        await reply_text(
            message,
            f"{E.INFO} <b>Usage:</b>\n"
            "• /filter &lt;trigger&gt; — Add filter with text reply\n"
            "• /filter &lt;trigger&gt; — Reply to a message to set as response\n"
            "• /stop &lt;trigger&gt; — Remove a filter",
            parse_mode=ParseMode.HTML,
        )
        return

    trigger = args[0].lower()
    chat_id = message.chat.id

    reply_body = None
    buttons = []
    media_type = None
    media_id = None

    if message.reply_to_message:
        reply_msg = message.reply_to_message
        reply_body = reply_msg.text or reply_msg.caption or ""

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
    elif len(args) > 1:
        reply_body = " ".join(args[1:])
    else:
        await reply_text(
            message,
            f"{E.ERROR} Provide a trigger word and reply to a message, or add text after the trigger."
        )
        return

    buttons_json = json.dumps(buttons) if buttons else None
    await asyncio.to_thread(
        db.add_filter, chat_id, trigger, reply_body, buttons_json, media_type, media_id
    )

    await reply_text(
        message,
        f"{E.CHECK} Filter set for <b>{trigger}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def stop_filter(message: Message, bot: Bot, args: list):
    """Handle /stop — remove a filter."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.",
            parse_mode=ParseMode.HTML)
        return

    if not await _is_admin(message, bot):
        await reply_text(message, f"{E.ERROR} You need admin rights to manage filters.",
            parse_mode=ParseMode.HTML)
        return

    if not args:
        await reply_text(message, f"{E.INFO} Usage: /stop &lt;trigger&gt;", parse_mode=ParseMode.HTML)
        return

    trigger = args[0].lower()
    chat_id = message.chat.id

    if await asyncio.to_thread(db.remove_filter, chat_id, trigger):
        await reply_text(message, f"{E.CHECK} Filter <b>{trigger}</b> removed.", parse_mode=ParseMode.HTML)
    else:
        await reply_text(message, f"{E.ERROR} Filter not found.",
            parse_mode=ParseMode.HTML)


async def filters_list(message: Message):
    """Handle /filters — list all filters in chat."""
    if message.chat.type == "private":
        await reply_text(message, f"{E.ERROR} This command only works in groups.")
        return

    chat_id = message.chat.id
    filters_data = await asyncio.to_thread(db.get_filters, chat_id)

    if not filters_data:
        await reply_text(message, f"{E.INFO} No filters set in this chat.",
            parse_mode=ParseMode.HTML)
        return

    trigger_list = "\n".join([f"• <code>{f['trigger_word']}</code>" for f in filters_data])
    await reply_text(
        message,
        f"{E.SETTINGS} <b>Active Filters ({len(filters_data)}):</b>\n{trigger_list}",
        parse_mode=ParseMode.HTML,
    )


async def check_filters(message: Message):
    """Check incoming messages against filters."""
    if not message or message.chat.type == "private":
        return

    chat_id = message.chat.id
    filters_data = await asyncio.to_thread(db.get_filters, chat_id)
    if not filters_data:
        return

    text = (message.text or message.caption or "").lower()

    for f in filters_data:
        trigger = f["trigger_word"]
        if trigger in text:
            reply_markup = _get_reply_buttons(f.get("buttons_json"))
            media_type = f.get("media_type")
            media_id = f.get("media_id")
            reply_body = f.get("reply_text", "")

            try:
                if media_type == "photo":
                    await reply_photo(message, photo=media_id, caption=reply_body, reply_markup=reply_markup)
                elif media_type == "sticker":
                    await reply_sticker(message, sticker=media_id)
                    if reply_body:
                        await reply_text(message, reply_body, reply_markup=reply_markup)
                elif media_type == "document":
                    await reply_document(message, document=media_id, caption=reply_body, reply_markup=reply_markup)
                elif media_type == "animation":
                    await reply_animation(message, animation=media_id, caption=reply_body, reply_markup=reply_markup)
                elif media_type == "video":
                    await reply_video(message, video=media_id, caption=reply_body, reply_markup=reply_markup)
                elif media_type == "voice":
                    await reply_voice(message, voice=media_id, caption=reply_body, reply_markup=reply_markup)
                elif media_type == "audio":
                    await reply_audio(message, audio=media_id, caption=reply_body, reply_markup=reply_markup)
                elif reply_body:
                    await reply_text(message, reply_body, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"Filter reply error: {e}")
            break


# ── Module setup ─────────────────────────────────────────
def setup() -> list:
    """Register filter commands and message handler."""
    on("message", add_filter, flt=and_f(cmd("filter"), GROUPS))
    on("message", stop_filter, flt=and_f(cmd("stop"), GROUPS))
    on("message", filters_list, flt=and_f(cmd("filters"), GROUPS))
    on("message", check_filters, group=1,
       flt=and_f(or_f(F.text, F.caption), ~COMMAND))

    return ["filter", "stop", "filters"]
