"""Test module — Demonstrates colored buttons via pure PTB.

Sends a message with colored inline buttons and handles callback queries.
"""

import logging

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, filters
from telegram.constants import ParseMode

from bot.keyboards.colored import btn_primary, btn_success, btn_danger, btn_default, build_keyboard

logger = logging.getLogger(__name__)


def _build_color_buttons():
    """Build the color test buttons."""
    return [
        [btn_primary("Primary (Blue)", "color:primary")],
        [btn_success("Success (Green)", "color:success"),
         btn_danger("Danger (Red)", "color:danger")],
        [btn_default("Default (White)", "color:default")],
    ]


async def testcolors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /testcolors — Send a message with colored buttons."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    buttons = _build_color_buttons()
    text = "<b>Colored Buttons Test</b>\n\nClick a button to see the color:"

    try:
        await update.message.reply_text(
            text,
            reply_markup=build_keyboard(buttons),
            parse_mode=ParseMode.HTML,
        )
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries from colored buttons."""
    query = update.callback_query
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
            await query.edit_message_text(
                text=new_text,
                reply_markup=build_keyboard(buttons),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")


def setup(app: Application) -> list:
    """Register test commands."""
    app.add_handler(CommandHandler("testcolors", testcolors_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(handle_color_callback, pattern=r"^color:"))
    return ["/testcolors", "color:* callbacks"]
