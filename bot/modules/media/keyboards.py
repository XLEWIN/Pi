"""Inline keyboards for the media module (plain text + EID icons).

The settings layout mirrors the owner's MEDIA DL screenshot: platform
toggles first, then content type, quality/size, captions/source, and
progress — every button colored via the shared btn_* helpers.
"""

from __future__ import annotations

from typing import List, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.emojis import EID
from bot.keyboards.colored import btn_danger, btn_primary, btn_success, btn_url


def open_on_ig(url: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if url:
        rows.append([btn_url("Open", url, icon_emoji_id=EID.WEB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(st: dict) -> InlineKeyboardMarkup:
    """Full /mediasettings board from a settings dict (see _media_defaults)."""

    def yn(field: str) -> bool:
        return bool(st.get(field, 1))

    def tog(label: str, field: str, on: bool, cb: str) -> InlineKeyboardButton:
        return btn_success(
            f"{label}: {'On' if on else 'Off'}",
            cb,
            icon_emoji_id=EID.CHECK if on else EID.CROSS,
        )

    quality = str(st.get("quality") or "auto").upper()
    max_mb = int(st.get("max_mb") or 50)
    captions = str(st.get("captions") or "short").capitalize()
    rows: List[List[InlineKeyboardButton]] = [
        [tog("YouTube", "yt_enabled", yn("yt_enabled"), "ig:set:yt"),
         tog("TikTok", "tt_enabled", yn("tt_enabled"), "ig:set:tt")],
        [tog("Videos", "videos", yn("videos"), "ig:set:videos"),
         tog("Shorts", "shorts", yn("shorts"), "ig:set:shorts")],
        [btn_primary(f"Quality: {quality}", "ig:set:quality", icon_emoji_id=EID.SETTINGS),
         btn_primary(f"File Size: {max_mb} MB", "ig:set:maxmb", icon_emoji_id=EID.FOLDER)],
        [btn_primary(f"Captions: {captions}", "ig:set:captions", icon_emoji_id=EID.INFO),
         tog("Delete Source", "delete_source", yn("delete_source"), "ig:set:delete")],
        [tog("Progress", "progress", yn("progress"), "ig:set:progress"),
         btn_danger("Close", "ig:close", icon_emoji_id=EID.CROSS)],
    ]
    # Row 0 of the screenshot's "Status / Auto" line lives on the legacy
    # auto button — keep it reachable as a compact top row.
    auto_on = bool(st.get("auto_download", 1))
    rows.insert(
        0,
        [tog("Auto", "auto_download", auto_on, "ig:set:auto")],
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def error_keyboard(url: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if url:
        rows.append([btn_url("Open link", url, icon_emoji_id=EID.WEB)])
    rows.append([btn_danger("Close", "ig:close", icon_emoji_id=EID.CROSS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
