"""Inline keyboards for Instagram module (plain text + EID icons)."""

from __future__ import annotations

from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.emojis import EID
from bot.keyboards.colored import btn_danger, btn_primary, btn_success, btn_url


def open_on_ig(url: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if url:
        rows.append([btn_url("Open on Instagram", url, icon_emoji_id=EID.WEB)])
    return InlineKeyboardMarkup(rows)


def settings_keyboard(auto: bool, max_items: int) -> InlineKeyboardMarkup:
    auto_label = "Auto: On" if auto else "Auto: Off"
    return InlineKeyboardMarkup(
        [
            [
                btn_success(
                    auto_label,
                    "ig:set:auto",
                    icon_emoji_id=EID.CHECK if auto else EID.CROSS,
                )
            ],
            [
                btn_primary(f"Max items: {max_items}", "ig:set:max", icon_emoji_id=EID.SETTINGS),
                btn_danger("Close", "ig:close", icon_emoji_id=EID.CROSS),
            ],
        ]
    )


def error_keyboard(url: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if url:
        rows.append([btn_url("Open link", url, icon_emoji_id=EID.WEB)])
    rows.append([btn_danger("Close", "ig:close", icon_emoji_id=EID.CROSS)])
    return InlineKeyboardMarkup(rows)
