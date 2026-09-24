"""Colored inline keyboards (Bot API 9.4+ style via api_kwargs).

Uses Telegram's native button styling:
- primary  → Blue
- success  → Green
- danger   → Red

Also supports icon_custom_emoji_id for custom emoji icons on buttons.
"""

from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ── Button builders ──────────────────────────────────────
def btn_primary(text: str, data: str, icon_emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """Blue/primary colored button."""
    kwargs = {"style": "primary"}
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    return InlineKeyboardButton(text, callback_data=data, api_kwargs=kwargs)


def btn_success(text: str, data: str, icon_emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """Green/success colored button."""
    kwargs = {"style": "success"}
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    return InlineKeyboardButton(text, callback_data=data, api_kwargs=kwargs)


def btn_danger(text: str, data: str, icon_emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """Red/danger colored button."""
    kwargs = {"style": "danger"}
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    return InlineKeyboardButton(text, callback_data=data, api_kwargs=kwargs)


def btn_default(text: str, data: str, icon_emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """Default/white colored button."""
    kwargs = {}
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    return InlineKeyboardButton(text, callback_data=data, api_kwargs=kwargs if kwargs else None)


def btn_url(text: str, url: str, icon_emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """URL button."""
    kwargs = {}
    if icon_emoji_id:
        kwargs["icon_custom_emoji_id"] = icon_emoji_id
    return InlineKeyboardButton(text, url=url, api_kwargs=kwargs if kwargs else None)


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
