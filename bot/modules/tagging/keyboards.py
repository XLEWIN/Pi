"""Tagging inline keyboards — settings cycles, Stop, Close.

Button text is plain text (no tg-emoji); icons use icon_emoji_id.
Callback data lives under the `tag:` prefix (group 0 handler).
"""

from __future__ import annotations

from telegram import InlineKeyboardMarkup

from bot.emojis import EID
from bot.keyboards.colored import (
    btn_danger,
    btn_default,
    btn_primary,
    build_keyboard,
)

from . import settings as settings_mod
from .models import TagSettings


def _value_label(key: str, st: TagSettings) -> str:
    if key == "mode":
        return settings_mod.MODE_LABELS.get(st.mode, st.mode)
    if key == "window_hours":
        return settings_mod.window_label(st.window_hours)
    if key == "max_mentions":
        return settings_mod.max_label(st.max_mentions)
    if key == "batch_size":
        return str(st.batch_size)
    if key == "send_mode":
        return settings_mod.SEND_LABELS.get(st.send_mode, st.send_mode)
    if key == "registry_mode":
        return settings_mod.REGISTRY_LABELS.get(
            st.registry_mode, st.registry_mode
        )
    return str(getattr(st, key, ""))


def settings_keyboard(st: TagSettings) -> InlineKeyboardMarkup:
    """Six cycle buttons in pairs + Close."""
    def cycle_btn(key: str, label: str):
        return btn_primary(
            f"{label} ▸ {_value_label(key, st)}",
            f"tag:set:{key}",
            icon_emoji_id=EID.SETTINGS,
        )

    rows = [
        [cycle_btn("mode", "Mode"), cycle_btn("window_hours", "Window")],
        [cycle_btn("max_mentions", "Max"), cycle_btn("batch_size", "Batch")],
        [cycle_btn("send_mode", "Send"), cycle_btn("registry_mode", "Registry")],
        [btn_default("Close", "card:close", icon_emoji_id=EID.CROSS)],
    ]
    return build_keyboard(rows)


def progress_keyboard() -> InlineKeyboardMarkup:
    """Stop button shown under the live progress card."""
    return build_keyboard(
        [[btn_danger("Stop", "tag:abort", icon_emoji_id=EID.CROSS)]]
    )


def stats_keyboard() -> InlineKeyboardMarkup:
    """Close button for /tagstats."""
    return build_keyboard(
        [[btn_default("Close", "card:close", icon_emoji_id=EID.CROSS)]]
    )
