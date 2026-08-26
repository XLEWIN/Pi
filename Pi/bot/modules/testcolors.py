"""Test module - Demonstrates colored buttons via Telethon MTProto.

Only callback buttons support colors (not URL buttons).
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telethon import events

from bot.telethon_client import get_client, btn_primary, btn_success, btn_danger, btn_default, build_keyboard

logger = logging.getLogger(__name__)


async def testcolors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /testcolors - Send a message with colored buttons via Telethon."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("This command only works in groups.")
        return

    telethon_client = get_client()
    if not telethon_client:
        await update.message.reply_text("Telethon client not available.")
        return

    chat_id = update.effective_chat.id

    # Build colored buttons
    buttons = [
        [btn_primary("Primary (Blue)", "color:primary")],
        [btn_success("Success (Green)", "color:success"),
         btn_danger("Danger (Red)", "color:danger")],
        [btn_default("Default (White)", "color:default")],
    ]

    try:
        await telethon_client.send_message(
            entity=chat_id,
            message="<b>Colored Buttons Test</b>\n\nClick a button to see the color:",
            buttons=build_keyboard(buttons),
            parse_mode="html",
        )
        # Delete the original command message
        await update.message.delete()
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_color_callback(event):
    """Handle callback queries from colored buttons."""
    data = event.data.decode("utf-8") if isinstance(event.data, bytes) else event.data

    if data.startswith("color:"):
        color = data.split(":")[1]
        await event.answer(f"You clicked the {color} button!")
        
        colors = {
            "primary": "Blue",
            "success": "Green",
            "danger": "Red",
            "default": "White",
        }
        
        buttons = [
            [btn_primary("Primary (Blue)", "color:primary")],
            [btn_success("Success (Green)", "color:success"),
             btn_danger("Danger (Red)", "color:danger")],
            [btn_default("Default (White)", "color:default")],
        ]

        # Use edit with positional text, then buttons
        new_text = f"<b>Colored Buttons Test</b>\n\nYou clicked: <b>{colors.get(color, color)}</b>"
        await event.edit(new_text, buttons=build_keyboard(buttons))


def setup(app: Application) -> list:
    """Register test commands."""
    app.add_handler(CommandHandler("testcolors", testcolors_command, filters=filters.ChatType.GROUPS))

    # Telethon callback handler will be registered after client init
    # (done in main.py post_init)
    return ["testcolors"]


def register_telethon_handlers():
    """Register Telethon callback handlers (call after client init)."""
    telethon_client = get_client()
    if telethon_client:
        telethon_client.on(events.CallbackQuery(pattern=b"color:"))(handle_color_callback)
