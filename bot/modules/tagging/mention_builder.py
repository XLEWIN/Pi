"""Mention construction — one HTML anchor per user.

Format (plain 👤 character, NOT a tg-emoji: custom-emoji tags would add
thousands of entities to a 150-mention batch and risk payload bloat):

    👤 <a href="tg://user?id=123">Name</a>
"""

from __future__ import annotations

from html import escape

from . import config
from .utils import clean_display_name

MENTION_ICON = "👤"


def build_mention(
    user_id: int,
    display_name: str,
    *,
    max_name: int = config.NAME_MAX,
) -> str:
    """Escaped, length-capped anchor for one user."""
    name = clean_display_name(display_name, user_id, max_name)
    return f'{MENTION_ICON} <a href="tg://user?id={user_id}">{escape(name)}</a>'


def mentions_length(mentions: list[str]) -> int:
    """UTF-16 length when the mentions are joined with newlines."""
    if not mentions:
        return 0
    total = sum(len(m.encode("utf-16-le")) // 2 for m in mentions)
    return total + (len(mentions) - 1)  # newline separators
