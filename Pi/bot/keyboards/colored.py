"""Colored inline keyboards (Bot API 9.4+ style via api_kwargs).

Uses Telegram's native button styling:
- primary  → Blue
- success  → Green
- danger   → Red
"""

from typing import List
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ── Button builders ──────────────────────────────────────
def btn_primary(text: str, data: str) -> InlineKeyboardButton:
    """Blue/primary colored button."""
    return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": "primary"})


def btn_success(text: str, data: str) -> InlineKeyboardButton:
    """Green/success colored button."""
    return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": "success"})


def btn_danger(text: str, data: str) -> InlineKeyboardButton:
    """Red/danger colored button."""
    return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": "danger"})


def btn_default(text: str, data: str) -> InlineKeyboardButton:
    """Default/white colored button."""
    return InlineKeyboardButton(text, callback_data=data)


def btn_url(text: str, url: str) -> InlineKeyboardButton:
    """URL button."""
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
    """Send a message with colored buttons via Bot API."""
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
