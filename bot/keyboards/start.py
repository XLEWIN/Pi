"""Inline keyboard for the /start menu.

Layout:  2 upper buttons · 1 middle button · 2 lower buttons
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.constants import (
    CB_DASHBOARD,
    CB_HELP,
    URL_ADD_TO_GROUP,
    URL_NETWORK,
    URL_OFFICIAL_CHANNEL,
)


def start_keyboard() -> InlineKeyboardMarkup:
    """Build the 2-1-2 start menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Bot To Chat", url=URL_ADD_TO_GROUP),
            InlineKeyboardButton("📖 View Help Menu", callback_data=CB_HELP),
        ],
        [
            InlineKeyboardButton("🌐 Dashboard", callback_data=CB_DASHBOARD),
        ],
        [
            InlineKeyboardButton("📢 Official Channel", url=URL_OFFICIAL_CHANNEL),
            InlineKeyboardButton("🕸️ Network", url=URL_NETWORK),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)