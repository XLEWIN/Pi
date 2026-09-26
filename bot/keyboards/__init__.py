"""Inline keyboards package.

Provides colored inline button helpers via pure PTB.
"""

from bot.keyboards.colored import (
    btn_primary,
    btn_success,
    btn_danger,
    btn_default,
    btn_url,
    build_keyboard,
    send_colored_buttons,
)

__all__ = [
    "btn_primary",
    "btn_success",
    "btn_danger",
    "btn_default",
    "btn_url",
    "build_keyboard",
    "send_colored_buttons",
]
