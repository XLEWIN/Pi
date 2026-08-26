"""Pure PTB colored inline button helpers.

Telegram's Bot API doesn't support colored button backgrounds (that's a
premium MTProto feature). This module provides clean helpers that use
emoji-based visual indicators to achieve a similar look.

Usage:
    from bot.keyboards.colored import btn_primary, btn_success, build_keyboard
    from telegram import InlineKeyboardMarkup

    buttons = [
        [btn_primary("Help", "start:help"), btn_success("Dashboard", "start:dashboard")],
    ]
    markup = InlineKeyboardMarkup(build_keyboard(buttons))
"""

from typing import List, Optional, Union
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ── Button builders ──────────────────────────────────────
def btn_primary(text: str, data: str) -> InlineKeyboardButton:
    """Blue/primary styled button."""
    return InlineKeyboardButton(f"🔵 {text}", callback_data=data)


def btn_success(text: str, data: str) -> InlineKeyboardButton:
    """Green/success styled button."""
    return InlineKeyboardButton(f"🟢 {text}", callback_data=data)


def btn_danger(text: str, data: str) -> InlineKeyboardButton:
    """Red/danger styled button."""
    return InlineKeyboardButton(f"🔴 {text}", callback_data=data)


def btn_default(text: str, data: str) -> InlineKeyboardButton:
    """Default/white styled button."""
    return InlineKeyboardButton(f"⚪ {text}", callback_data=data)


def btn_url(text: str, url: str) -> InlineKeyboardButton:
    """URL button (no color indicator)."""
    return InlineKeyboardButton(text, url=url)


# ── Keyboard builder ─────────────────────────────────────
def build_keyboard(
    rows: List[List[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    """Build an InlineKeyboardMarkup from rows of buttons."""
    return InlineKeyboardMarkup(rows)


# ── Convenience send helpers ─────────────────────────────
async def send_colored_buttons(
    context,
    chat_id: int,
    text: str,
    buttons: List[List[InlineKeyboardButton]],
    parse_mode: str = "HTML",
    delete_original: bool = False,
    original_message=None,
):
    """Send a message with colored buttons via Bot API.

    Args:
        context: PTB ContextTypes.DEFAULT_TYPE
        chat_id: Target chat
        message: Message text
        buttons: 2D list of InlineKeyboardButton
        parse_mode: Parse mode for message text
        delete_original: If True, delete the original command message
        original_message: The original message to delete

    Returns:
        Sent Message or None on failure
    """
    try:
        reply_markup = build_keyboard(buttons)
        result = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        if delete_original and original_message:
            try:
                await original_message.delete()
            except Exception:
                pass
        return result
    except Exception as e:
        return None
