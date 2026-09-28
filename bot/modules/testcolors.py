"""Test module — Demonstrates colored buttons via Bot API 9.4+.

Sends a message with colored inline buttons and handles callback queries.
"""

import logging
import re

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.filters.logic import and_f
from aiogram.types import CallbackQuery, Message

from bot.keyboards.colored import btn_primary, btn_success, btn_danger, btn_default, build_keyboard
from bot.pipeline import on, GROUPS, cmd
from bot.reply import reply_text

logger = logging.getLogger(__name__)


def _build_color_buttons():
    """Build the color test buttons."""
    return [
        [btn_primary("Primary (Blue)", "color:primary")],
        [btn_success("Success (Green)", "color:success"),
         btn_danger("Danger (Red)", "color:danger")],
        [btn_default("Default (White)", "color:default")],
    ]


async def testcolors_command(message: Message):
    """Handle /testcolors — Send a message with colored buttons."""
    if message.chat.type == "private":
        await reply_text(message, "This command only works in groups.")
        return

    buttons = _build_color_buttons()
    text = "<b>Colored Buttons Test</b>\n\nClick a button to see the color:"

    try:
        await reply_text(
            message,
            text,
            reply_markup=build_keyboard(buttons),
            parse_mode=ParseMode.HTML,
        )
        await message.delete()
    except Exception as e:
        await reply_text(message, f"Error: {e}")


async def handle_color_callback(callback_query: CallbackQuery):
    """Handle callback queries from colored buttons."""
    query = callback_query
    data = query.data

    if data.startswith("color:"):
        color = data.split(":")[1]
        colors = {
            "primary": "Blue",
            "success": "Green",
            "danger": "Red",
            "default": "White",
        }
        await query.answer(f"You clicked the {colors.get(color, color)} button!")

        buttons = _build_color_buttons()
        new_text = f"<b>Colored Buttons Test</b>\n\nYou clicked: <b>{colors.get(color, color)}</b>"

        try:
            await query.message.edit_text(
                text=new_text,
                reply_markup=build_keyboard(buttons),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")


def setup() -> list:
    """Register test commands."""
    on("message", testcolors_command, flt=and_f(cmd("testcolors"), GROUPS))
    on("callback_query", handle_color_callback, flt=F.data.regexp(re.compile(r"^color:")))
    return ["/testcolors", "color:* callbacks"]
